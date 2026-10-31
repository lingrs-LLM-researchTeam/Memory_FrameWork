"""Audit regressions through production read, write and recovery interfaces."""
import time
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_recall_reliability import engine  # noqa: F401
from tests.test_memory_v02_invariants import SCOPE, mutate, source
from astrbot_plugin_livingmemory.core.tools.memory_evidence_reader import read_memory_evidence_core
from astrbot_plugin_livingmemory.core.retrieval.dual_route_retriever import DualRouteRetriever
from astrbot_plugin_livingmemory.core.retrieval.hybrid_retriever import HybridResult
from astrbot_plugin_livingmemory.core.models.conversation_models import Message
from astrbot_plugin_livingmemory.storage.conversation_store import ConversationStore


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle", [
    {"expires_at": 1}, {"valid_to": 1}, {"valid_from": time.time() + 86400},
])
async def test_deep_read_rejects_non_atomized_invisible_parent(engine, lifecycle):
    mid = await engine.add_memory(content="expired private fact", session_id=SCOPE, metadata=lifecycle)
    result = await read_memory_evidence_core(
        memory_engine=engine, recall_session_id=SCOPE, recall_persona_id=None,
        memory_id=mid, include_source=True,
    )
    assert result["status"] != "success"
    assert "expired private fact" not in str(result)
    record = await engine.get_memory_for_recall(mid)
    assert await engine.resolve_live_facts(mid, record["text"], record["metadata"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("graph_hit", [False, True])
async def test_dual_route_always_reloads_primary_state(graph_hit):
    stale = HybridResult(7, 1, 1, 1, None, "unpublished", {"status": "active", "session_id": SCOPE})
    primary = AsyncMock(return_value={
        "id": 7, "text": "unpublished", "metadata": {"status": "pending_source", "session_id": SCOPE}
    })
    dual = DualRouteRetriever(
        SimpleNamespace(search=AsyncMock(return_value=[stale])),
        SimpleNamespace(search=AsyncMock(return_value=[SimpleNamespace(
            doc_id=7, final_score=1, score_breakdown={}, rrf_score=1,
            keyword_score=1, vector_score=1,
        )] if graph_hit else [])),
        primary,
    )
    assert await dual.search("unpublished", session_id=SCOPE) == []
    primary.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_legacy_archive_without_atom_history_restores_to_quarantine(engine):
    mid = await engine.add_memory(content="legacy forgotten fact", session_id=SCOPE)
    await mutate(engine, mid, status="archived")  # old archive with no preservation marker
    assert await engine.restore_memory(mid)
    record = await engine.get_memory_for_recall(mid)
    assert record["metadata"]["status"] == "quarantined"
    assert await engine.search_memories("forgotten", session_id=SCOPE) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["livingmemory:global", "livingmemory:user:bot-b:uid-b"])
async def test_shared_scope_requires_authorized_source_binding(engine, scope):
    with pytest.raises(ValueError, match="source|scope"):
        await engine.add_memory(content="foreign evidence", session_id=scope, source_messages=source())


@pytest.mark.asyncio
async def test_replayed_transport_id_cannot_change_sender(tmp_path):
    store = ConversationStore(str(tmp_path / "messages.db"))
    await store.initialize()
    try:
        fields = dict(id=None, session_id=SCOPE, role="user", content="same words",
                      timestamp=1700000000, metadata={"source_uid": "same-transport"})
        await store.add_message(Message(**fields, sender_id="alice"))
        with pytest.raises(ValueError, match="revision|identity|transport"):
            await store.add_message(Message(**fields, sender_id="bob"))
    finally:
        await store.close()


async def recorded_source(store):
    mid = await store.add_message(Message(
        id=None, session_id=SCOPE, role="user", content="Alice likes tea",
        sender_id="alice", timestamp=1700000000,
        metadata={"source_uid": "msg-alice", "platform_message_id": "42"},
    ))
    return mid, [row.to_dict() for row in await store.get_messages(SCOPE)]


@pytest.mark.asyncio
async def test_source_delete_blocks_all_reads_before_outbox_delivery(engine, tmp_path):
    store = ConversationStore(str(tmp_path / "raw.db"))
    await store.initialize()
    try:
        await engine.bind_source_store(store)
        _, sources = await recorded_source(store)
        mid = await engine.add_memory(content="Alice likes tea", session_id=SCOPE, source_messages=sources)
        assert await engine.search_memories("tea", session_id=SCOPE)
        evidence = await read_memory_evidence_core(
            memory_engine=engine, recall_session_id=SCOPE, recall_persona_id=None,
            memory_id=mid, include_source=True,
        )
        assert evidence["source_messages"][0]["source_uid"] == "msg-alice"
        assert evidence["source_messages"][0]["platform_message_id"] == "42"
        await store.delete_session_messages(SCOPE)
        # Deliberately do not dispatch the outbox: raw authority closes the gap.
        assert await engine.search_memories("tea", session_id=SCOPE) == []
        evidence = await read_memory_evidence_core(
            memory_engine=engine, recall_session_id=SCOPE, recall_persona_id=None,
            memory_id=mid, include_source=True,
        )
        assert "Alice likes tea" not in str(evidence)
        await store.dispatch_source_events(engine.consume_source_event)
        assert (await engine.get_memory_for_recall(mid))["metadata"]["status"] == "source_revoked"
        assert not await engine.restore_memory(mid)
        with pytest.raises((ValueError, RuntimeError)):
            await engine.add_memory(content="retry", session_id=SCOPE, source_messages=sources)
    finally:
        engine.source_store = None
        await store.close()


@pytest.mark.asyncio
async def test_source_revision_invalidates_old_summary(engine, tmp_path):
    store = ConversationStore(str(tmp_path / "raw.db"))
    await store.initialize()
    try:
        engine.source_store = store
        message_id, sources = await recorded_source(store)
        mid = await engine.add_memory(content="Alice likes tea", session_id=SCOPE, source_messages=sources)
        await store.connection.execute("UPDATE messages SET content=? WHERE id=?", ("Alice likes coffee", message_id))
        await store.connection.commit()
        assert await engine.search_memories("tea", session_id=SCOPE) == []
        revised = [row.to_dict() for row in await store.get_messages(SCOPE)]
        assert revised[0]["metadata"]["revision"] == 1
        newer = await engine.add_memory(content="Alice likes coffee", session_id=SCOPE, source_messages=revised)
        assert newer != mid
        await store.dispatch_source_events(engine.consume_source_event)
        assert (await engine.get_memory_for_recall(newer))["metadata"]["status"] == "active"
        assert (await engine.get_memory_for_recall(mid))["metadata"]["status"] == "source_revoked"
    finally:
        engine.source_store = None
        await store.close()


@pytest.mark.asyncio
async def test_retention_keeps_snapshot_but_later_recall_revokes_it(engine, tmp_path):
    store = ConversationStore(str(tmp_path / "raw.db"))
    await store.initialize()
    try:
        engine.source_store = store
        _, sources = await recorded_source(store)
        mid = await engine.add_memory(content="Alice likes tea", session_id=SCOPE, source_messages=sources)
        await store.connection.execute(
            "UPDATE sessions SET metadata=? WHERE session_id=?", (json.dumps({"last_summarized_index": 1}), SCOPE)
        )
        await store.connection.commit()
        assert await store.trim_session_messages(SCOPE, 1) == 1
        assert await engine.memory_sources_current(mid)
        await store.revoke_source(SCOPE, "msg-alice")
        assert not await engine.memory_sources_current(mid)
    finally:
        engine.source_store = None
        await store.close()


@pytest.mark.asyncio
async def test_source_outbox_and_message_rollback_together(tmp_path, monkeypatch):
    store = ConversationStore(str(tmp_path / "raw.db"))
    await store.initialize()
    commit = store.connection.commit
    monkeypatch.setattr(store.connection, "commit", AsyncMock(side_effect=OSError("disk full")))
    try:
        with pytest.raises(OSError):
            await recorded_source(store)
        for table in ("messages", "source_ledger", "source_outbox"):
            cursor = await store.connection.execute(f"SELECT COUNT(*) FROM {table}")
            assert (await cursor.fetchone())[0] == 0
    finally:
        monkeypatch.setattr(store.connection, "commit", commit)
        await store.close()


@pytest.mark.asyncio
async def test_completed_write_journal_does_not_retain_replay_text(engine):
    mid = await engine.add_memory(content="sensitive body", session_id=SCOPE, source_messages=source("sensitive source"))
    cursor = await engine.db_connection.execute("SELECT payload FROM memory_write_ops WHERE memory_id=? AND status='completed'", (mid,))
    payloads = await cursor.fetchall()
    assert payloads
    assert all("sensitive" not in row[0] for row in payloads)


@pytest.mark.asyncio
async def test_graph_is_not_published_before_source_persistence(engine, monkeypatch):
    graph_write = AsyncMock(wraps=engine.graph_memory_manager.index_memory)
    monkeypatch.setattr(engine.graph_memory_manager, "index_memory", graph_write)
    monkeypatch.setattr(engine, "save_memory_source", AsyncMock(side_effect=OSError("source disk full")))
    with pytest.raises(OSError):
        await engine.add_memory(content="not yet sourced", session_id=SCOPE, source_messages=source())
    graph_write.assert_not_awaited()
    assert await engine.search_memories("sourced", session_id=SCOPE) == []
