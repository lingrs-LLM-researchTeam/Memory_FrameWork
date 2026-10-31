"""Regression tests for the independently reproduced v0.3 review findings."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from astrbot_plugin_livingmemory.core.managers.conversation_manager import ConversationManager
from astrbot_plugin_livingmemory.core.models.conversation_models import Message
from astrbot_plugin_livingmemory.core.summary_recovery import SummaryRecovery
from astrbot_plugin_livingmemory.storage.conversation_store import ConversationStore
from tests.test_recall_reliability import engine  # noqa: F401

SCOPE = "bot-a:GroupMessage:42"


def message(uid, text=None):
    return Message(
        id=0, session_id=SCOPE, role="user", sender_id="uid-a",
        content=text or uid, platform="aiocqhttp", group_id="42",
        metadata={"source_uid": uid, "reflection_context": {
            "persona_id": "p", "memory_scope": SCOPE,
        }},
    )


@pytest_asyncio.fixture
async def store(tmp_path):
    instance = ConversationStore(str(tmp_path / "raw.db"))
    await instance.initialize()
    try:
        yield instance
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_pruned_transport_replay_is_idempotent_but_substitution_is_rejected(store):
    original = await store.add_message(message("one"))
    manager = ConversationManager(store)
    await manager.update_session_metadata(SCOPE, "last_summarized_index", 1)
    assert await store.trim_session_messages(SCOPE, 1) == 1
    assert await store.add_message(message("one")) == original
    assert await store.get_message_count(SCOPE) == 0
    with pytest.raises(ValueError):
        await store.add_message(message("one", "substituted"))


@pytest.mark.asyncio
async def test_revoke_repairs_cursor_pending_and_invalidates_inflight_work(store):
    manager = ConversationManager(store)
    for uid in "ABCDEF":
        await store.add_message(message(uid))
    await manager.update_session_metadata(SCOPE, "last_summarized_index", 3)
    await manager.update_session_metadata(SCOPE, "pending_summary", {
        "start_index": 3, "end_index": 6,
    })
    old = await manager.get_session_revision(SCOPE)
    await manager.revoke_source(SCOPE, "B")
    assert await manager.get_session_metadata(SCOPE, "last_summarized_index", 0) == 2
    assert await manager.get_session_metadata(SCOPE, "pending_summary", None) == {
        "start_index": 2, "end_index": 5,
    }
    assert [m.content for m in await manager.get_messages_range(SCOPE, 2, 5)] == ["D", "E", "F"]
    assert not await manager.update_session_metadata_if_revision(SCOPE, "last_summarized_index", 5, old)


@pytest.mark.asyncio
async def test_persona_failure_keeps_raw_input_and_persistent_unresolved_marker(store):
    manager = ConversationManager(store)
    manager.reflection_context_resolver = AsyncMock(side_effect=RuntimeError("preferences unavailable"))
    event = SimpleNamespace(
        unified_msg_origin=SCOPE, get_sender_id=lambda: "uid-a",
        get_sender_name=lambda: "Alice", get_platform_name=lambda: "aiocqhttp",
        get_extra=lambda key, default=None: default,
        message_obj=SimpleNamespace(message_id="42", message=[]),
    )
    await manager.add_message_from_event(event, "user", "synthetic input")
    saved = await store.get_messages(SCOPE)
    assert len(saved) == 1
    assert saved[0].metadata["reflection_context_status"] == "unresolved"
    cursor = await store.connection.execute("SELECT COUNT(*) FROM reflection_dirty")
    assert (await cursor.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_failed_window_is_not_replanned_until_explicit_retry(store, monkeypatch):
    import astrbot_plugin_livingmemory.core.summary_recovery as module
    monkeypatch.setattr(module, "is_session_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(module, "is_plugin_enabled_for_session", AsyncMock(return_value=True))
    manager = ConversationManager(store)
    for uid in "ABCD":
        await store.add_message(message(uid))
    reflection = SimpleNamespace(
        conversation_manager=manager,
        config_manager=SimpleNamespace(get=lambda key, default=None: {
            "reflection_engine.summary_trigger_rounds": 1,
        }.get(key, default)),
        _storage_task=AsyncMock(side_effect=OSError("provider unavailable")),
        _storage_state_lock=asyncio.Lock(), _storage_sessions_inflight=set(),
        _find_window_split_boundary=lambda *args, **kwargs: None,
    )
    recovery = SummaryRecovery(reflection)
    store.enqueue_summary = AsyncMock(wraps=store.enqueue_summary)
    for _ in range(3):
        with pytest.raises(OSError):
            await recovery.recover_once()
    calls = store.enqueue_summary.await_count
    for _ in range(3):
        await recovery.recover_once()
    assert store.enqueue_summary.await_count == calls
    assert reflection._storage_task.await_count == 3
    cursor = await store.connection.execute("SELECT id FROM summary_jobs WHERE status='failed'")
    job_id = (await cursor.fetchone())[0]
    assert await store.retry_summary_job(SCOPE, job_id)
    with pytest.raises(OSError):
        await recovery.recover_once()
    assert reflection._storage_task.await_count == 4


@pytest.mark.asyncio
async def test_unresolved_capture_is_resolved_after_restart(store, monkeypatch):
    import astrbot_plugin_livingmemory.core.summary_recovery as module
    monkeypatch.setattr(module, "is_session_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(module, "is_plugin_enabled_for_session", AsyncMock(return_value=True))
    for uid in "AB":
        item = message(uid)
        item.metadata.pop("reflection_context")
        item.metadata["reflection_context_status"] = "unresolved"
        await store.add_message(item)
    await store.close()
    await store.initialize()
    manager = ConversationManager(store)
    manager.reflection_context_resolver = AsyncMock(return_value={"persona_id": "p", "memory_scope": SCOPE})
    reflection = SimpleNamespace(
        conversation_manager=manager,
        config_manager=SimpleNamespace(get=lambda key, default=None: {
            "reflection_engine.summary_trigger_rounds": 1,
        }.get(key, default)),
        _storage_sessions_inflight=set(), _find_window_split_boundary=lambda *a, **k: None,
    )
    await SummaryRecovery(reflection).plan_dirty_sessions()
    assert len(await store.pending_summary_jobs()) == 1
    assert all(m.metadata["reflection_context_status"] == "resolved" for m in await store.get_messages(SCOPE))


@pytest.mark.asyncio
async def test_user_summary_keeps_raw_cursor_but_only_summarizes_that_user(store, engine):
    from astrbot_plugin_livingmemory.core.event_handler_modules.memory_reflection import MemoryReflection
    scope = "livingmemory:user:bot-a:uid-a"
    engine.config["shared_source_sessions"] = [SCOPE]
    await engine.bind_source_store(store)
    manager = ConversationManager(store)
    for uid, role, sender in [("A", "user", "uid-a"), ("B", "assistant", "bot")]:
        item = message(uid)
        item.role, item.sender_id = role, sender
        item.metadata["reflection_context"]["memory_scope"] = scope
        await store.add_message(item)
    processor = SimpleNamespace(
        process_conversation=AsyncMock(return_value=("user A only", {}, 0.5)),
        classify_atoms_from_metadata=lambda **kwargs: [],
    )
    reflection = MemoryReflection(
        None, SimpleNamespace(get=lambda key, default=None: default), engine, processor,
        manager, None, set(), set(), asyncio.Lock(),
    )
    await reflection._storage_task(SCOPE, await store.get_messages(SCOPE), "p", 0, 2, 0, memory_scope=scope, expected_revision=0)
    assert [m.role for m in processor.process_conversation.call_args.kwargs["messages"]] == ["user"]
    assert await manager.get_session_metadata(SCOPE, "last_summarized_index", 0) == 2
    cursor = await engine.db_connection.execute("SELECT role,sender_id FROM memory_source_links")
    assert [tuple(row) for row in await cursor.fetchall()] == [("user", "uid-a")]


@pytest.mark.asyncio
async def test_bound_authority_cannot_be_unset_to_read_revoked_source(store, engine):
    await engine.bind_source_store(store)
    await store.add_message(message("A"))
    mid = await engine.add_memory(content="synthetic", session_id=SCOPE, persona_id="p",
                                  source_messages=[m.to_dict() for m in await store.get_messages(SCOPE)])
    await store.revoke_source(SCOPE, "A")
    engine.source_store = None
    with pytest.raises(RuntimeError, match="authority"):
        await engine.memory_sources_current(mid)


@pytest.mark.asyncio
async def test_revoke_legacy_local_row_repairs_cursor_without_transport_backfill(store):
    # A v0.3 migration left the raw source_uid column null while ledger UID was local.
    await store.connection.execute("DROP TRIGGER source_insert")
    await store.connection.execute(
        "INSERT INTO sessions(session_id,platform,created_at,last_active_at,message_count,metadata)"
        " VALUES(?,?,0,0,1,?)", (SCOPE, "qq", '{"last_summarized_index":1}'),
    )
    await store.connection.execute(
        "INSERT INTO messages(session_id,role,content,sender_id,timestamp) VALUES(?,?,?,?,0)",
        (SCOPE, "user", "legacy synthetic", "uid-a"),
    )
    await store.connection.commit()
    await store.initialize_source_ledger()
    saved = (await store.get_messages(SCOPE))[0]
    await store.revoke_source(SCOPE, f"local:{SCOPE}:{saved.id}")
    assert await store.get_message_count(SCOPE) == 0
    assert (await store.get_session(SCOPE)).metadata["last_summarized_index"] == 0


@pytest.mark.asyncio
async def test_companion_outage_keeps_outbox_retryable(store):
    await store.add_message(message("A"))
    await store.revoke_source(SCOPE, "A")
    consumer = AsyncMock(side_effect=RuntimeError("companion unavailable"))
    await store.dispatch_source_events(consumer)
    row = await (await store.connection.execute("SELECT status,next_attempt_at FROM source_outbox")).fetchone()
    assert row["status"] == "pending" and row["next_attempt_at"] > 0
    assert await store.dispatch_source_events(consumer) == 0
    await store.connection.execute("UPDATE source_outbox SET next_attempt_at=0")
    await store.connection.commit()
    consumer.side_effect = None
    assert await store.dispatch_source_events(consumer) == 1


@pytest.mark.asyncio
async def test_retention_of_upgraded_ledger_keeps_real_transport_metadata(store):
    item = message("transport")
    item.metadata["platform_message_id"] = "42"
    original = await store.add_message(item)
    # A v0.3 ledger acquired the new nullable column during upgrade.
    await store.connection.execute("UPDATE source_ledger SET platform_message_id=NULL")
    await store.connection.commit()
    manager = ConversationManager(store)
    await manager.update_session_metadata(SCOPE, "last_summarized_index", 1)
    await store.trim_session_messages(SCOPE, 1)
    assert await store.add_message(item) == original


@pytest.mark.asyncio
async def test_auxiliary_cleanup_keeps_failed_blocked_and_undelivered_records(store):
    for uid in "ABC":
        await store.add_message(message(uid))
    assert (await (await store.connection.execute("SELECT COUNT(*) FROM source_outbox")).fetchone())[0] == 0
    await store.revoke_source(SCOPE, "A")
    await store.connection.execute("UPDATE source_outbox SET status='completed',created_at=0")
    await store.revoke_source(SCOPE, "B")
    await store.connection.execute("UPDATE source_outbox SET created_at=0")
    for i, status in [(101, "completed"), (102, "failed"), (103, "cancelled")]:
        await store.connection.execute(
            "INSERT INTO summary_jobs(id,job_key,session_id,payload,status,updated_at) VALUES(?,?,?,'{}',?,0)",
            (i, str(i), SCOPE, status),
        )
    await store.connection.execute("UPDATE reflection_dirty SET blocked_job_id=103")
    await store.connection.commit()
    assert await store.cleanup_completed_auxiliary() == {"outbox": 1, "jobs": 1}
    assert (await (await store.connection.execute("SELECT status FROM source_outbox")).fetchone())[0] == "pending"
    assert [row[0] for row in await (await store.connection.execute("SELECT id FROM summary_jobs ORDER BY id")).fetchall()] == [102, 103]
    assert (await (await store.connection.execute("SELECT COUNT(*) FROM source_ledger")).fetchone())[0] == 3
