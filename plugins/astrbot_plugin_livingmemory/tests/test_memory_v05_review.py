"""Cross-plugin lifecycle and window contracts exercised through real stores."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from tests.test_memory_v04_review import message, SCOPE, store  # noqa: F401
from astrbot_plugin_livingmemory.core.managers.conversation_manager import ConversationManager
from astrbot_plugin_livingmemory.core.summary_recovery import SummaryRecovery
from astrbot_plugin_livingmemory.core.source_companions import notify_source_companions


@pytest.mark.asyncio
async def test_absent_consumer_keeps_event_pending_then_delivers(store):
    stars = []
    context = SimpleNamespace(get_all_stars=lambda: stars)
    async def deliver(event):
        await notify_source_companions(context, event)
    await store.revoke_source(SCOPE, "missing")
    await store.dispatch_source_events(deliver)
    row = await (await store.connection.execute("SELECT status FROM source_outbox")).fetchone()
    assert row[0] == "pending"
    callback = AsyncMock()
    stars.append(SimpleNamespace(name="self_learning", star_cls=SimpleNamespace(on_memory_source_event=callback)))
    await store.connection.execute("UPDATE source_outbox SET next_attempt_at=0")
    await store.connection.commit()
    await store.dispatch_source_events(deliver)
    callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_recall_does_not_invalidate_jobs_or_session_revision(store):
    manager = ConversationManager(store)
    for uid in "AB":
        await store.add_message(message(uid))
    revision = await manager.get_session_revision(SCOPE)
    job = await store.enqueue_summary(SCOPE, await store.get_messages(SCOPE), "p", 0, 2, SCOPE, revision)
    await manager.revoke_source(SCOPE, "never-captured")
    assert await manager.get_session_revision(SCOPE) == revision
    assert [j["id"] for j in await store.pending_summary_jobs()] == [job]


@pytest.mark.asyncio
async def test_unaffected_running_window_survives_later_recall(store):
    manager = ConversationManager(store)
    for uid in "ABCD":
        await store.add_message(message(uid))
    rows = await store.get_messages(SCOPE)
    job = await store.enqueue_summary(SCOPE, rows[:2], "p", 0, 2, SCOPE, 0)
    entered, resume = asyncio.Event(), asyncio.Event()
    async def persist(session, messages, persona, start, end, attempts, **kwargs):
        entered.set()
        await resume.wait()
        assert await manager.update_session_metadata_if_revision(
            session, "last_summarized_index", end, kwargs["expected_revision"])
    reflection = SimpleNamespace(conversation_manager=manager, _storage_task=persist)
    recovery = SummaryRecovery(reflection)
    recovery.messages_allowed = lambda messages: True
    task = asyncio.create_task(recovery.run_job(job))
    await asyncio.wait_for(entered.wait(), 3)
    try:
        await manager.revoke_source(SCOPE, "D")
    finally:
        resume.set()
    await task
    row = await (await store.connection.execute("SELECT status FROM summary_jobs WHERE id=?", (job,))).fetchone()
    assert row[0] == "completed"
    assert await manager.get_session_metadata(SCOPE, "last_summarized_index", 0) == 2


@pytest.mark.asyncio
async def test_deletion_before_window_cancels_shifted_job(store):
    manager = ConversationManager(store)
    for uid in "ABCD":
        await store.add_message(message(uid))
    rows = await store.get_messages(SCOPE)
    job = await store.enqueue_summary(SCOPE, rows[2:], "p", 2, 4, SCOPE, 0)
    await manager.revoke_source(SCOPE, "A")
    row = await (await store.connection.execute("SELECT status FROM summary_jobs WHERE id=?", (job,))).fetchone()
    assert row[0] == "cancelled"


@pytest.mark.asyncio
async def test_empty_other_hook_candidates_cannot_suppress_generation():
    from astrbot_plugin_livingmemory.learning.config import PluginConfig
    from astrbot_plugin_livingmemory.learning.services.hooks.llm_hook_handler import LLMHookHandler
    producer = AsyncMock(return_value=[{"memory_id": 7}])
    gate = AsyncMock(return_value=["verified"])
    authority = SimpleNamespace(find_evidence_candidates=producer, govern_external_context=gate)
    handler = LLMHookHandler(PluginConfig(require_memory_authority=True, enable_llm_hooks=True),
        Mock(), Mock(), Mock(), Mock(), Mock(), Mock(), {},
        feature_delegation=SimpleNamespace(memory_plugin=lambda: SimpleNamespace(star_cls=authority)))
    extras = {"livingmemory_evidence_candidates": []}
    event = SimpleNamespace(get_extra=lambda k, default=None: extras.get(k, default),
                            set_extra=lambda k, v: extras.__setitem__(k, v))
    request = SimpleNamespace(prompt="tea", extra_user_content_parts=[], system_prompt="")
    await handler.handle(event, request)
    producer.assert_awaited_once_with(event, "tea")
    assert len(request.extra_user_content_parts) == 1


@pytest.mark.asyncio
async def test_unknown_recall_keeps_tombstone_without_group_quarantine(tmp_path):
    from sqlalchemy import select
    from astrbot_plugin_livingmemory.learning.config import PluginConfig
    from astrbot_plugin_livingmemory.learning.core.database.engine import DatabaseEngine
    from astrbot_plugin_livingmemory.learning.services.database.facades.message_facade import MessageFacade
    from astrbot_plugin_livingmemory.learning.services.database.source_lifecycle import consume_source_event
    from astrbot_plugin_livingmemory.learning.models.orm.learning import PersonaLearningReview
    from astrbot_plugin_livingmemory.learning.models.orm.source_lifecycle import LearningSource, InvalidatedLearningGroup
    engine = DatabaseEngine("sqlite:///" + (tmp_path / "learning.db").as_posix())
    engine.require_memory_authority = True
    await engine.create_tables(enable_auto_migration=True)
    manager = SimpleNamespace(engine=engine, is_ready=True)
    try:
        async with engine.get_session() as session:
            session.add(PersonaLearningReview(group_id="42", timestamp=time.time(), update_type="style", status="approved"))
            await session.commit()
        await consume_source_event(manager, dict(session_id=SCOPE, source_uid="never", revision=1, state="revoked"))
        async with engine.get_session() as session:
            assert await session.get(InvalidatedLearningGroup, "42") is None
            assert (await session.get(LearningSource, (SCOPE, "never"))).state == "revoked"
            assert len((await session.execute(select(PersonaLearningReview))).scalars().all()) == 1
        facade = MessageFacade(engine, PluginConfig(require_memory_authority=True))
        assert await facade.save_raw_message(dict(sender_id="alice", message="late", group_id="42",
            timestamp=time.time(), source_scope=SCOPE, source_uid="never")) == 0
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_reloaded_consumer_reconciles_even_after_completed_outbox_cleanup(store):
    from astrbot_plugin_livingmemory.core.source_companions import SourceCompanionDelivery
    await store.add_message(message("A"))
    await store.revoke_source(SCOPE, "A")
    # Simulate v0.4 completing a delivery while the consumer was absent.
    await store.connection.execute("DELETE FROM source_outbox")
    await store.connection.commit()
    first, second = AsyncMock(), AsyncMock()
    stars = [SimpleNamespace(name="self_learning", star_cls=SimpleNamespace(on_memory_source_event=first))]
    delivery = SourceCompanionDelivery(SimpleNamespace(get_all_stars=lambda: stars), store)
    await delivery.reconcile()
    await delivery.reconcile()
    assert first.await_count == 1
    stars[0].star_cls = SimpleNamespace(on_memory_source_event=second)
    await delivery.reconcile()
    assert second.await_count == 1
    assert second.call_args.args[0]["source_uid"] == "A"


@pytest.mark.asyncio
async def test_reconcile_failure_retries_without_acknowledging_instance(store):
    from astrbot_plugin_livingmemory.core.source_companions import SourceCompanionDelivery
    await store.revoke_source(SCOPE, "A")
    receive = AsyncMock(side_effect=RuntimeError("not ready"))
    star = SimpleNamespace(name="self_learning", star_cls=SimpleNamespace(on_memory_source_event=receive))
    delivery = SourceCompanionDelivery(SimpleNamespace(get_all_stars=lambda: [star]), store)
    with pytest.raises(RuntimeError):
        await delivery.reconcile()
    receive.side_effect = None
    await delivery.reconcile()
    assert receive.await_count == 2


@pytest.mark.asyncio
async def test_candidate_cache_is_query_scoped_and_empty_results_are_cached():
    from astrbot_plugin_livingmemory.core.evidence_candidates import find_candidate_ids
    extras = {}
    event = SimpleNamespace(get_extra=lambda k, d=None: extras.get(k, d),
                            set_extra=lambda k, v: extras.__setitem__(k, v))
    engine = SimpleNamespace(search_memories=AsyncMock(side_effect=[[], [SimpleNamespace(doc_id=7)]]))
    args = dict(k=5, session_id=SCOPE, persona_id="p")
    first = await asyncio.gather(*[find_candidate_ids(event, engine, query="empty", **args) for _ in range(2)])
    assert first == [[], []]
    assert engine.search_memories.await_count == 1
    assert await find_candidate_ids(event, engine, query="tea", **args) == [{"memory_id": 7}]
    assert engine.search_memories.await_count == 2


@pytest.mark.asyncio
async def test_window_guard_rejects_own_recall_and_reset_but_accepts_append(store):
    manager = ConversationManager(store)
    for uid in "AB":
        await store.add_message(message(uid))
    job = await store.enqueue_summary(SCOPE, await store.get_messages(SCOPE), "p", 0, 2, SCOPE, 0)
    row = await (await store.connection.execute("SELECT payload FROM summary_jobs WHERE id=?", (job,))).fetchone()
    guard = manager.summary_window_revision(SCOPE, json.loads(row[0]))
    await store.add_message(message("C"))
    assert await manager.summary_revision_current(SCOPE, guard)
    await manager.bump_session_revision(SCOPE)
    assert not await manager.update_session_metadata_if_revision(SCOPE, "last_summarized_index", 2, guard)
    guard = manager.summary_window_revision(SCOPE, json.loads(row[0]))
    await manager.revoke_source(SCOPE, "B")
    assert not await manager.update_session_metadata_if_revision(SCOPE, "last_summarized_index", 2, guard)
