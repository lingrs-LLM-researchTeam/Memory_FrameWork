"""Boundary, capture and durable cursor regressions for fixed-time scheduling."""
import asyncio
from datetime import datetime
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_livingmemory.core import summary_recovery
from astrbot_plugin_livingmemory.core.event_handler_modules.memory_reflection import MemoryReflection
from astrbot_plugin_livingmemory.core.managers.conversation_manager import ConversationManager
from astrbot_plugin_livingmemory.core.summary_recovery import SummaryRecovery
from astrbot_plugin_livingmemory.core.summary_schedule import SHANGHAI, slot_bounds
from tests.test_memory_v04_review import message, SCOPE, store  # noqa: F401


def at(hour, minute=0, day=4):
    return datetime(2026, 11, day, hour, minute, tzinfo=SHANGHAI).timestamp()


@pytest.mark.parametrize("end", [at(8), at(13), at(18, 30), at(0, day=5)])
def test_boundary_is_exclusive_for_messages(end):
    assert slot_bounds(end - 0.01)[1] == end
    assert slot_bounds(end)[0] == end


@pytest.fixture
def scheduled(store, monkeypatch):
    monkeypatch.setattr(summary_recovery, "is_session_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(summary_recovery, "is_plugin_enabled_for_session", AsyncMock(return_value=True))
    manager = ConversationManager(store)
    seen = []

    async def persist(session, messages, persona, start, end, attempts, **kw):
        assert await manager.update_session_metadata_if_revision(
            session, "last_summarized_index", end, kw["expected_revision"])
        seen.append((persona, [m.content for m in messages]))

    reflection = NS(conversation_manager=manager, config_manager={"reflection_engine.summary_trigger_mode": "daily_slots",
                                                                  "reflection_engine.slot_min_messages": 1},
                    _storage_state_lock=asyncio.Lock(), _storage_sessions_inflight=set(),
                    _storage_task=persist, _schedule_consolidation=Mock(),
                    _find_window_split_boundary=MemoryReflection._find_window_split_boundary)
    return SummaryRecovery(reflection), seen


async def add(store, uid, stamp, persona="p"):
    item = message(uid)
    item.timestamp = stamp
    item.metadata["reflection_context"]["persona_id"] = persona
    await store.add_message(item)


@pytest.mark.asyncio
async def test_small_windows_quiet_edges_restart_and_missed_slots(store, scheduled):
    recovery, seen = scheduled
    for uid, stamp, persona in [("a", at(7), "p"), ("b", at(8), "p"), ("c", at(12), "q"),
                                ("d", at(13), "q"), ("e", at(18, 30), "q"),
                                ("f", at(0, day=5), "q")]:
        await add(store, uid, stamp, persona)
    await recovery._recover_slot(at(8))
    assert seen == [("p", ["a"])]
    await store.close()
    await store.initialize()
    fresh = SummaryRecovery(recovery.reflection)
    await fresh._recover_slot(at(8))
    assert len(seen) == 1
    await fresh._recover_slot(at(0, day=5))
    assert seen == [("p", ["a"]), ("p", ["b"]), ("q", ["c"]), ("q", ["d"]), ("q", ["e"])]
    await fresh._recover_slot(at(8, day=5))
    await fresh._recover_slot(at(13, day=5))
    assert seen[-1] == ("q", ["f"]) and len(seen) == 6


@pytest.mark.asyncio
async def test_pending_narrow_window_retried_before_new_inputs(store, scheduled):
    recovery, seen = scheduled
    await add(store, "a", at(7))
    persist = recovery.reflection._storage_task
    recovery.reflection._storage_task = AsyncMock(side_effect=OSError("fixture"))
    await recovery._recover_slot(at(8))
    manager = recovery.reflection.conversation_manager
    assert await manager.get_session_metadata(SCOPE, "last_summarized_index", 0) == 0
    await add(store, "b", at(9))
    recovery.reflection._storage_task = persist
    await recovery._recover_slot(at(8))
    assert not seen
    await recovery._recover_slot(at(13))
    assert seen == [("p", ["a"]), ("p", ["b"])]
    assert await manager.get_session_metadata(SCOPE, "last_summarized_index", 0) == 2


@pytest.mark.asyncio
async def test_accumulated_pages_revoke_and_mode_switch(store, scheduled):
    recovery, seen = scheduled
    for n in range(205):
        await add(store, str(n), at(7) + n)
    manager = recovery.reflection.conversation_manager
    await manager.revoke_source(SCOPE, "0")
    await recovery._recover_slot(at(8))
    assert [len(items) for _, items in seen] == [200, 4]
    assert set(x for _, items in seen for x in items) == {str(n) for n in range(1, 205)}
    recovery.reflection.config_manager.clear()
    for n in range(20):
        await add(store, "new" + str(n), at(9) + n)
    await recovery.recover_once()
    assert [len(items) for _, items in seen] == [200, 4, 20]


@pytest.mark.asyncio
async def test_event_captures_response_then_waits_for_clock(monkeypatch):
    from astrbot_plugin_livingmemory.core.event_handler_modules import memory_reflection as handler
    monkeypatch.setattr(handler, "is_event_memory_allowed", lambda *_: True)
    manager = NS(add_message_from_event=AsyncMock(), get_session_info=AsyncMock())
    reflection = MemoryReflection(None, {"reflection_engine.summary_trigger_mode": "daily_slots"},
                                  None, None, manager, None, set(), set(), asyncio.Lock())
    event = NS(unified_msg_origin=SCOPE, get_message_type=lambda: handler.MessageType.GROUP_MESSAGE)
    response = NS(role="assistant", completion_text="有效回复", tools_call_name=None, tools_call_extra_content=None)
    await reflection.handle_memory_reflection(event, response)
    manager.add_message_from_event.assert_awaited_once()
    manager.get_session_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_late_platform_message_after_commit_is_processed_once(store, scheduled):
    recovery, seen = scheduled
    await add(store, "original", at(7, 59))
    await recovery._recover_slot(at(8))
    await add(store, "delayed", at(7, 48))
    await add(store, "current", at(9))
    await recovery._recover_slot(at(8))
    assert seen == [("p", ["original"])]
    await recovery._recover_slot(at(13))
    assert seen == [("p", ["original"]), ("p", ["delayed"]), ("p", ["current"])]
    manager = recovery.reflection.conversation_manager
    rows = await manager.get_messages_range(SCOPE, 0, 3)
    assert [m.timestamp for m in rows] == [at(7,59), at(7,48), at(9)]
    await manager.revoke_source(SCOPE, "delayed")
    assert await manager.get_session_metadata(SCOPE, "last_summarized_index", 0) == 2
    assert [m.content for m in await manager.get_messages_range(SCOPE, 0, 2)] == ["original", "current"]


@pytest.mark.asyncio
async def test_carried_round_job_waits_until_its_whole_window_is_due(store, scheduled):
    recovery, seen = scheduled
    await add(store, "before", at(7))
    await add(store, "after", at(8, 1))
    rows = await store.get_messages(SCOPE)
    rows.sort(key=lambda m: m.timestamp)
    await store.enqueue_summary(SCOPE, rows, "p", 0, 2, SCOPE, 0)
    await recovery._recover_slot(at(8))
    assert not seen
    await recovery._recover_slot(at(13))
    assert seen == [("p", ["before", "after"])]


@pytest.fixture
def low_flow(scheduled):
    recovery, seen = scheduled
    del recovery.reflection.config_manager["reflection_engine.slot_min_messages"]
    return recovery, seen


@pytest.mark.asyncio
async def test_sparse_slots_wait_without_jobs_then_settle_once_even_without_new_event(store, low_flow):
    recovery, seen = low_flow
    await recovery._recover_slot(at(8))
    await add(store, "a", at(12, 58))
    await recovery._recover_slot(at(13))
    await add(store, "b", at(13, 2))
    await recovery._recover_slot(at(18, 30))
    assert not seen
    assert not await store.pending_summary_jobs()
    assert await recovery.reflection.conversation_manager.get_session_metadata(SCOPE, "last_summarized_index", 0) == 0
    await store.close()
    await store.initialize()
    resumed = SummaryRecovery(recovery.reflection)
    # Midnight and 08:00 already elapsed during downtime. No further messages arrive.
    await resumed._recover_slot(at(8, day=5))
    await resumed._recover_slot(at(13, day=5))
    assert seen == [("p", ["a", "b"])]
    assert await store.completed_summary_slot() == at(13, day=5)


@pytest.mark.asyncio
async def test_daytime_threshold_counts_only_before_cutoff_and_enforces_persona_isolation(store, low_flow):
    recovery, seen = low_flow
    for i in range(19):
        await add(store, str(i), at(7, 59) + i, persona="p")
    await add(store, "twenty", at(8), persona="q")
    await recovery._recover_slot(at(8))
    assert not seen
    await recovery._recover_slot(at(13))
    assert seen == [("p", [str(i) for i in range(19)])]
    await add(store, "new day", at(0, day=5))
    await recovery._recover_slot(at(0, day=5))
    assert seen[-1] == ("q", ["twenty"])
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_threshold_and_page_tail_are_not_separate_small_calls(store, low_flow):
    recovery, seen = low_flow
    for i in range(201):
        await add(store, str(i), at(7) + i)
    await recovery._recover_slot(at(8))
    assert [len(items) for _, items in seen] == [200]
    for i in range(19):
        await add(store, "later" + str(i), at(8, 10) + i)
    await recovery._recover_slot(at(13))
    await recovery._recover_slot(at(0, day=5))
    assert [len(items) for _, items in seen] == [200, 20]
    assert seen[1][1][0] == "200"


@pytest.mark.asyncio
async def test_revoked_deferred_messages_do_not_trigger_midnight(store, low_flow):
    recovery, seen = low_flow
    await add(store, "removed", at(7))
    await recovery._recover_slot(at(8))
    await recovery.reflection.conversation_manager.revoke_source(SCOPE, "removed")
    await recovery._recover_slot(at(0, day=5))
    assert not seen
    assert not await store.pending_summary_jobs()
