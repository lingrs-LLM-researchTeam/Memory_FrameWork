"""Portable v0.2: real SQLite/FAISS safety and crash-replay regression tests."""

import asyncio
from dataclasses import asdict
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_recall_reliability import engine  # noqa: F401 (real core fixture)
from astrbot_plugin_livingmemory.core.models.memory_atom import AtomStatus, MemoryAtom
from astrbot_plugin_livingmemory.core.models.conversation_models import Message
from astrbot_plugin_livingmemory.core.memory_scope import is_event_memory_allowed, resolve_memory_scope
from astrbot_plugin_livingmemory.core.source_identity import source_metadata
from astrbot_plugin_livingmemory.core.utils.formatting import format_memories_for_injection
from astrbot_plugin_livingmemory.core.evidence_budget import (
    automatic_allowance, bounded_evidence_call, budget_for, charge_automatic,
)
from astrbot_plugin_livingmemory.storage.conversation_store import ConversationStore

SCOPE = "bot-a:GroupMessage:42"


def source(content="A reports a preference", scope=SCOPE):
    return [{
        "id": 1, "session_id": scope, "role": "user", "content": content,
        "sender_id": "uid-a", "sender_name": "same nickname", "timestamp": 1700000000,
        "metadata": {"source_uid": "transport-1", "platform_message_id": "msg-1"},
    }]


async def mutate(engine, memory_id, **updates):
    record = await engine.get_memory_for_recall(memory_id)
    metadata = {**record["metadata"], **updates}
    await engine.db_connection.execute(
        "UPDATE documents SET metadata = ? WHERE id = ?",
        (json.dumps(metadata), memory_id),
    )
    await engine.db_connection.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("status", "archived"), ("session_id", "bot-b:GroupMessage:42"),
    ("persona_id", "other"), ("expires_at", 1),
])
async def test_cached_hits_recheck_primary_visibility(engine, field, value):
    mid = await engine.add_memory(content="cache evidence", session_id=SCOPE, persona_id="p1")
    assert await engine.search_memories("cache evidence", session_id=SCOPE, persona_id="p1")
    generation = engine._search_cache_generation
    await mutate(engine, mid, **{field: value})
    assert engine._search_cache_generation == generation  # bypass invalidation
    assert await engine.search_memories("cache evidence", session_id=SCOPE, persona_id="p1") == []


@pytest.mark.asyncio
async def test_scope_metadata_and_atoms_cannot_override_trusted_scope(engine):
    atom = MemoryAtom(parent_memory_id=0, content="scope fact", session_id="foreign", persona_id="foreign")
    mid = await engine.add_memory(
        content="scope fact", session_id=SCOPE, persona_id="p1",
        metadata={"session_id": "foreign", "persona_id": "foreign"}, atoms=[atom],
    )
    record = await engine.get_memory_for_recall(mid)
    assert record["metadata"]["session_id"] == SCOPE
    stored = await engine.atom_store.get_by_parent(mid)
    assert stored[0].session_id == SCOPE and stored[0].persona_id == "p1"
    assert await engine.search_memories("scope", session_id="foreign") == []


@pytest.mark.asyncio
async def test_archive_restore_preserves_atom_identity_expiry_and_conflict(engine):
    atoms = [
        MemoryAtom(parent_memory_id=0, content="live fact", expires_at=time.time() + 86400),
        MemoryAtom(parent_memory_id=0, content="expired fact", expires_at=1),
        MemoryAtom(parent_memory_id=0, content="forgotten fact", status=AtomStatus.FORGOTTEN),
    ]
    mid = await engine.add_memory(
        content="live fact expired fact forgotten fact", session_id=SCOPE,
        atoms=atoms, metadata={"persona_summary": "expired fact forgotten fact"},
        source_messages=source(),
    )
    before = [asdict(atom) for atom in await engine.atom_store.get_by_parent(mid)]
    assert await engine.archive_memories([mid]) == 1
    assert await engine.search_memories("fact", session_id=SCOPE) == []
    assert await engine.atom_store.search_fts("live fact", session_id=SCOPE) == []
    assert await engine.restore_memory(mid)
    after = [asdict(atom) for atom in await engine.atom_store.get_by_parent(mid)]
    assert before == after
    engine.atom_enabled = False  # Extraction toggle must not disable lifecycle.
    hits = await engine.search_memories("fact", session_id=SCOPE)
    assert [hit.doc_id for hit in hits] == [mid]
    text = format_memories_for_injection([
        {"id": r.doc_id, "content": r.content, "metadata": r.metadata} for r in hits
    ])
    assert "live fact" in text
    assert "expired fact" not in text and "forgotten fact" not in text


@pytest.mark.asyncio
async def test_archive_index_failure_remains_invisible_and_repairable(engine, monkeypatch):
    mid = await engine.add_memory(content="archive crash", session_id=SCOPE)
    storage = engine.faiss_db.embedding_storage
    original = storage.delete
    monkeypatch.setattr(storage, "delete", AsyncMock(side_effect=OSError("disk failure")))
    assert await engine.archive_memories([mid]) == 1
    assert await engine.search_memories("archive crash", session_id=SCOPE) == []
    monkeypatch.setattr(storage, "delete", original)
    assert await engine._repair_incomplete_write_ops() >= 1
    assert (await engine.get_memory_for_recall(mid))["metadata"]["status"] == "archived"


@pytest.mark.asyncio
async def test_source_window_idempotency_survives_restart_and_soft_delete(engine):
    ids = await asyncio.gather(*[
        engine.add_memory(content=f"nondeterministic summary {i}", session_id=SCOPE, source_messages=source())
        for i in range(3)
    ])
    assert len(set(ids)) == 1
    await engine.close()
    await engine.graph_vector_db.initialize()
    await engine.initialize()
    await engine.atom_lifecycle_manager.stop()
    again = await engine.add_memory(content="retry after restart", session_id=SCOPE, source_messages=source())
    assert again == ids[0]
    await engine.archive_memories(ids[:1])
    assert await engine.add_memory(content="retry archived", session_id=SCOPE, source_messages=source()) == ids[0]
    assert (await engine.get_memory_for_recall(ids[0]))["metadata"]["status"] == "archived"


@pytest.mark.asyncio
async def test_source_replacement_does_not_delete_the_reused_original(engine):
    mid = await engine.add_memory(content="old summary", session_id=SCOPE, source_messages=source())
    revised = await engine.replace_memory(mid, content="corrected summary", importance=0.7, metadata={})
    assert revised != mid
    assert (await engine.get_memory_for_recall(revised))["text"] == "corrected summary"
    assert await engine.get_memory_for_recall(mid) is None
    retry = await engine.add_memory(content="late old summary", session_id=SCOPE, source_messages=source())
    assert retry == revised


@pytest.mark.asyncio
async def test_interrupted_source_write_replayed_before_publication(engine, monkeypatch):
    class PowerLoss(BaseException):
        pass

    original = engine.save_memory_source
    monkeypatch.setattr(engine, "save_memory_source", AsyncMock(side_effect=PowerLoss()))
    with pytest.raises(PowerLoss):
        await engine.add_memory(content="interrupted summary", session_id=SCOPE, source_messages=source())
    assert await engine.search_memories("interrupted", session_id=SCOPE) == []
    monkeypatch.setattr(engine, "save_memory_source", original)
    assert await engine._repair_incomplete_write_ops() >= 1
    mid = await engine.add_memory(content="retry", session_id=SCOPE, source_messages=source())
    from astrbot_plugin_livingmemory.core.memory_source import serialize_source_messages
    assert await engine.get_memory_source(mid) == serialize_source_messages(source())
    assert (await engine.get_memory_for_recall(mid))["metadata"]["status"] == "active"


@pytest.mark.asyncio
async def test_cross_conversation_source_is_rejected(engine):
    with pytest.raises(ValueError, match="does not match"):
        await engine.add_memory(content="foreign", session_id=SCOPE, source_messages=source(scope="other"))


@pytest.mark.asyncio
async def test_no_guess_migration_on_read(engine):
    engine._migrate_session_data_if_needed = AsyncMock()
    await engine.add_memory(content="legacy group", session_id="42")
    assert await engine.search_memories("legacy group", session_id=SCOPE) == []
    engine._migrate_session_data_if_needed.assert_not_awaited()


@pytest.mark.asyncio
async def test_transport_dedup_is_durable_and_scoped(tmp_path):
    path = str(tmp_path / "conversations.db")
    store = ConversationStore(path)
    await store.initialize()
    msg = Message(id=0, session_id=SCOPE, role="user", content="hello",
                  sender_id="a", timestamp=1, metadata={"source_uid": "m1"})
    first = await store.add_message(msg)
    await store.close()
    store = ConversationStore(path)
    await store.initialize()
    try:
        assert await store.add_message(msg) == first
        msg.content = "changed text with the same transport ID"
        with pytest.raises(ValueError, match="reconcile"):
            await store.add_message(msg)
        msg.content = "hello"
        msg.session_id = "bot-b:GroupMessage:42"
        assert await store.add_message(msg) != first
        assert (await store.get_session(SCOPE)).message_count == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_failed_message_insert_rolls_back_session_creation(tmp_path):
    store = ConversationStore(str(tmp_path / "rollback.db"))
    await store.initialize()
    try:
        await store.connection.execute(
            "CREATE TRIGGER fail_insert BEFORE INSERT ON messages BEGIN SELECT RAISE(ABORT, 'fault'); END"
        )
        await store.connection.commit()
        msg = Message(id=0, session_id=SCOPE, role="user", content="test", sender_id="a", timestamp=1)
        with pytest.raises(Exception, match="fault"):
            await store.add_message(msg)
        assert await store.get_session(SCOPE) is None
    finally:
        await store.close()


def test_nickname_cannot_impersonate_allowlist_identity():
    event = SimpleNamespace(
        unified_msg_origin=SCOPE, get_sender_id=lambda: "attacker",
        get_sender_name=lambda: "trusted-name", get_platform_name=lambda: "qq",
        get_group_id=lambda: "42",
    )
    config = {"access_control": {"whitelist_enabled": True, "allowed_ids": "owner",
                                "identity_aliases": "trusted-name=owner"}}
    assert not is_event_memory_allowed(config, event)
    assert not is_event_memory_allowed({}, SimpleNamespace(unified_msg_origin=""))
    config = {"filtering_settings": {"memory_scope_mode": "user"}}
    assert resolve_memory_scope(config, event) == "livingmemory:user:bot-a:attacker"


def test_transport_identity_keeps_quote_id_but_does_not_assign_it_to_bot():
    Reply = type("Reply", (), {})
    reply = Reply()
    reply.id = "quoted-id"
    event = SimpleNamespace(unified_msg_origin=SCOPE,
                            message_obj=SimpleNamespace(message_id="m1", message=[reply]))
    data = source_metadata(event, "user", "quoted response")
    assert data["reply_to_platform_message_id"] == "quoted-id"
    assert data["platform_message_id"] == "m1"
    assert "source_uid" not in source_metadata(event, "assistant", "bot response")


@pytest.mark.asyncio
async def test_automatic_and_parallel_tool_outputs_share_one_ceiling():
    config = {"agent_tools": {"max_total_evidence_bytes": 7000}}
    event = SimpleNamespace()
    ctx = SimpleNamespace(context=SimpleNamespace(event=event))
    evidence = format_memories_for_injection([
        {"id": i, "content": "事实" * 100, "metadata": {}} for i in range(8)
    ], max_tokens=automatic_allowance(event, config))
    charge_automatic(event, config, evidence)

    class Tool:
        config_manager = config

        @bounded_evidence_call
        async def call(self, context):
            await asyncio.sleep(0)
            return json.dumps({"count": 3, "results": [{"id": i, "text": "内容" * 250} for i in range(3)]}, ensure_ascii=False)

    outputs = await asyncio.gather(*[Tool().call(ctx) for _ in range(4)])
    total = sum(len(s.encode("utf-8")) for s in [evidence, *outputs])
    assert total <= 7000
    assert total == budget_for(event, config)["used"]
    assert len(evidence.encode("utf-8")) <= 3500


def test_relative_dates_use_source_time_and_past_events_are_not_plans():
    from datetime import datetime
    from astrbot_plugin_livingmemory.core.processors.atom_classifier import classify_atoms, _parse_event_time
    from astrbot_plugin_livingmemory.core.models.memory_atom import AtomType

    anchor = datetime(2024, 6, 10, 12).timestamp()
    atoms = classify_atoms(["明天去开会", "昨天完成讨论"], reference_time=anchor)
    assert atoms[0].event_time == anchor + 86400
    assert atoms[0].expires_at < time.time()
    assert atoms[1].atom_type == AtomType.EPISODIC
    assert _parse_event_time("大后天去", anchor) == anchor + 3 * 86400
    assert _parse_event_time("2月30日", anchor) is None
    assert datetime.fromtimestamp(_parse_event_time("下周一", anchor)).date().isoformat() == "2024-06-17"


@pytest.mark.asyncio
async def test_persona_lookup_failure_does_not_become_unfiltered_access(monkeypatch):
    from astrbot_plugin_livingmemory.core import utils
    monkeypatch.setattr(utils, "sp", SimpleNamespace(get_async=AsyncMock(side_effect=OSError("unavailable"))))
    with pytest.raises(RuntimeError, match="人格"):
        await utils.get_persona_id(SimpleNamespace(), SimpleNamespace(unified_msg_origin=SCOPE))


@pytest.mark.asyncio
async def test_reranker_receives_live_evidence_only(engine):
    atoms = [
        MemoryAtom(parent_memory_id=0, content="allowed fact", expires_at=time.time() + 10000),
        MemoryAtom(parent_memory_id=0, content="expired private fact", expires_at=1),
    ]
    await engine.add_memory(content="allowed fact expired private fact", session_id=SCOPE, atoms=atoms)
    retriever = engine.hybrid_retriever
    calls = []

    async def capture(query, results, k):
        calls.extend(result.content for result in results)
        return results[:k]

    retriever.rerank_enabled = True
    retriever.rerank_provider_resolver = lambda: object()
    retriever._apply_rerank = capture
    assert await retriever.search("fact", k=3, session_id=SCOPE)
    assert calls and all("expired private fact" not in text for text in calls)
