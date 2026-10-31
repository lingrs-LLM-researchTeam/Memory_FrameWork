"""Evidence, durable recovery and reviewed-publication invariants."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_recall_reliability import engine  # noqa: F401
from tests.test_memory_v02_invariants import SCOPE, source
from tests.test_memory_v03_audit import recorded_source
from astrbot_plugin_livingmemory.core.claim_evidence import validate_claims, claim_text
from astrbot_plugin_livingmemory.core.memory_source import source_reference
from astrbot_plugin_livingmemory.core.models.conversation_models import Message
from astrbot_plugin_livingmemory.core.summary_recovery import SummaryRecovery
from astrbot_plugin_livingmemory.core.source_events import recalled_source
from astrbot_plugin_livingmemory.storage.conversation_store import ConversationStore


def candidate(text="I like tea", value="tea", uid="transport-1"):
    return {
        "subject_id": "uid-a", "predicate": "drink", "value": value,
        "qualifier": "self_report", "assertion": "untrusted paraphrase",
        "evidence": [{"source_uid": uid, "quote": text}],
    }


def test_claims_require_exact_user_quotes_and_stable_subjects():
    sources = source("I like tea")
    assert not validate_claims([candidate("invented")], sources)
    assistant = source("I like tea")
    assistant[0]["role"] = "assistant"
    assert not validate_claims([candidate()], assistant)
    claim = validate_claims([candidate()], sources)[0]
    assert claim["subject_id"] == "uid-a"
    assert claim["reporter_ids"] == ["uid-a"]
    assert "untrusted paraphrase" not in claim_text(claim)
    assert "I like tea" in claim_text(claim)
    different = {**candidate(), "subject_id": "uid-b"}
    sources += [{**source("hello")[0], "sender_id": "uid-b", "metadata": {"source_uid": "other"}}]
    unknown = validate_claims([different], sources)[0]
    assert unknown["subject_id"] is None
    assert unknown["qualifier"] == "reported"
    assert not validate_claims([{**candidate(), "valid_from": 20, "valid_to": 10}], sources)


async def add_claim(engine, text, value, uid, *, persona="p", scope=SCOPE):
    sources = source(text)
    sources[0]["session_id"] = scope
    sources[0]["metadata"]["source_uid"] = uid
    claims = validate_claims([candidate(text, value, uid)], sources)
    return await engine.add_memory(
        content=claim_text(claims[0]), session_id=scope, persona_id=persona,
        metadata={"claim_schema_version": 1, "claims": claims},
        source_messages=sources,
    )


@pytest.mark.asyncio
async def test_conflicting_claims_preserved_and_persona_isolated(engine):
    one = await add_claim(engine, "I like tea", "tea", "one")
    two = await add_claim(engine, "I like coffee", "coffee", "two")
    other = await add_claim(engine, "I like milk", "milk", "three", persona="other")
    claims = await engine.live_claims(one)
    assert claims[0]["conflicts_with"] == [two]
    assert (await engine.live_claims(other))[0]["conflicts_with"] == []
    await engine.invalidate_source(SCOPE, "two")
    assert (await engine.live_claims(one))[0]["conflicts_with"] == []
    assert (await engine.get_memory_for_recall(one))["metadata"]["status"] == "active"


@pytest.mark.asyncio
async def test_publication_requires_review_and_still_checks_source(engine, monkeypatch):
    mid = await add_claim(engine, "I like tea", "tea", "tea")
    target = "bot-a:GroupMessage:88"
    pid = await engine.propose_knowledge(mid, SCOPE, target, "p", "admin")
    assert await engine.search_reviewed_knowledge("tea", target, "p") == []
    with pytest.raises(ValueError):
        await engine.review_knowledge(pid, target, "p", "admin", True)
    await engine.review_knowledge(pid, SCOPE, "p", "admin", True)
    # Publishing one record must not search/rerank the rest of the source group.
    monkeypatch.setattr(engine, "search_memories", AsyncMock(side_effect=AssertionError("scope-wide search")))
    assert len(await engine.search_reviewed_knowledge("tea", target, "p")) == 1
    assert await engine.search_reviewed_knowledge("tea", target, "other") == []
    assert await engine.search_reviewed_knowledge("tea", "other", "p") == []
    await engine.invalidate_source(SCOPE, "tea")
    assert await engine.search_reviewed_knowledge("tea", target, "p") == []


@pytest.mark.asyncio
async def test_forged_snapshot_cannot_reuse_real_source_identity(engine, tmp_path):
    store = ConversationStore(str(tmp_path / "raw.db"))
    await store.initialize()
    try:
        engine.source_store = store
        _, sources = await recorded_source(store)
        sources[0]["content"] = "malicious substituted body"
        with pytest.raises(ValueError):
            await engine.add_memory(content="forged", session_id=SCOPE, source_messages=sources)
        sources = [m.to_dict() for m in await store.get_messages(SCOPE)]
        await store.connection.execute("UPDATE sessions SET metadata=? WHERE session_id=?",
                                       (json.dumps({"last_summarized_index": 1}), SCOPE))
        await store.connection.commit()
        assert await store.trim_session_messages(SCOPE, 1) == 1
        assert await store.sources_current([source_reference(sources[0])])
        sources[0]["content"] = "forged after retention"
        assert not await store.sources_current([source_reference(sources[0])])
    finally:
        engine.source_store = None
        await store.close()


def test_recall_event_uses_transport_notice_not_message_text():
    from astrbot_plugin_livingmemory.core.source_identity import fingerprint
    event = SimpleNamespace(
        unified_msg_origin=SCOPE,
        message_obj=SimpleNamespace(raw_message={"post_type": "notice", "notice_type": "group_recall", "message_id": 42}),
    )
    assert recalled_source(event) == (SCOPE, fingerprint([SCOPE, "user", "42"]))
    event.message_obj.raw_message = {"post_type": "message", "notice_type": "group_recall", "message_id": 42}
    assert recalled_source(event) is None


@pytest.mark.asyncio
async def test_raw_input_dirty_marker_and_restartable_body_free_jobs(tmp_path):
    path = str(tmp_path / "raw.db")
    store = ConversationStore(path)
    await store.initialize()
    message = Message(
        id=None, session_id=SCOPE, role="user", sender_id="uid-a",
        content="private job body", metadata={"source_uid": "job-source",
        "reflection_context": {"persona_id": "p", "memory_scope": SCOPE}},
    )
    try:
        await store.add_message(message)
        messages = await store.get_messages(SCOPE)
        job = await store.enqueue_summary(SCOPE, messages, "p", 0, 1, SCOPE, 0)
        assert await store.enqueue_summary(SCOPE, messages, "p", 0, 1, SCOPE, 0) == job
        cursor = await store.connection.execute("SELECT generation FROM reflection_dirty WHERE session_id=?", (SCOPE,))
        assert (await cursor.fetchone())[0] == 1
        await store.mark_summary_job(job, "running")
        await store.close()
        store = ConversationStore(path)
        await store.initialize()
        pending = await store.pending_summary_jobs()
        assert pending[0]["id"] == job and pending[0]["attempts"] == 1
        assert "private job body" not in pending[0]["payload"]
        assert (await store.summary_job_messages(json.loads(pending[0]["payload"])))[0].content == "private job body"
        for _ in range(2):
            await store.mark_summary_job(job, "running")
            await store.mark_summary_job(job, "pending", error="test")
        assert await store.pending_summary_jobs() == []
        cursor = await store.connection.execute("SELECT status FROM summary_jobs WHERE id=?", (job,))
        assert (await cursor.fetchone())[0] == "failed"
        await store.revoke_source(SCOPE, "job-source")
        assert await store.summary_job_messages(json.loads(pending[0]["payload"])) is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_summary_recovery_advances_only_after_storage_success(tmp_path):
    store = ConversationStore(str(tmp_path / "jobs.db"))
    await store.initialize()
    try:
        await recorded_source(store)
        messages = await store.get_messages(SCOPE)
        job = await store.enqueue_summary(SCOPE, messages, "p", 0, 1, SCOPE, 0)
        from astrbot_plugin_livingmemory.core.managers.conversation_manager import ConversationManager
        manager = ConversationManager(store)
        reflection = SimpleNamespace(conversation_manager=manager, config_manager={},
                                     _storage_task=AsyncMock(side_effect=OSError("offline")))
        recovery = SummaryRecovery(reflection)
        with pytest.raises(OSError):
            await recovery.run_job(job)
        pending = await store.pending_summary_jobs()
        assert pending[0]["id"] == job
        async def commit(*args, **kwargs):
            assert await manager.update_session_metadata_if_revision(
                SCOPE, "last_summarized_index", 1, kwargs["expected_revision"])
        reflection._storage_task = AsyncMock(side_effect=commit)
        await recovery.run_job(job)
        assert await store.pending_summary_jobs() == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_archived_memory_cannot_restore_before_revocation_outbox_is_delivered(engine, tmp_path):
    store = ConversationStore(str(tmp_path / "raw.db"))
    await store.initialize()
    try:
        engine.source_store = store
        _, sources = await recorded_source(store)
        mid = await engine.add_memory(content="tea", session_id=SCOPE, source_messages=sources)
        assert await engine.archive_memories([mid]) == 1
        await store.revoke_source(SCOPE, "msg-alice")
        assert not await engine.restore_memory(mid)
        assert (await engine.get_memory_for_recall(mid))["metadata"]["status"] == "archived"
    finally:
        engine.source_store = None
        await store.close()
