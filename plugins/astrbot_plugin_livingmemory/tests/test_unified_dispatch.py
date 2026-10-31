"""Actual single Star dispatch, cancellation, shutdown and direct binding contracts."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from astrbot_plugin_livingmemory.main import LivingMemoryPlugin
from astrbot_plugin_livingmemory.learning.core.feature_delegation import FeatureDelegation


def plugin():
    p = LivingMemoryPlugin.__new__(LivingMemoryPlugin)
    p.modules = SimpleNamespace(call=AsyncMock(), stop=AsyncMock())
    p._ensure_plugin_ready = AsyncMock(return_value=(True, 'ready'))
    p.event_handler = SimpleNamespace(handle_memory_recall=AsyncMock(), handle_session_reset=AsyncMock())
    return p


@pytest.mark.asyncio
async def test_memory_then_learning_request_order():
    p, order = plugin(), []
    async def memory(*args): order.append('memory')
    async def learning(*args): order.append('learning')
    p.event_handler.handle_memory_recall.side_effect = memory
    p.modules.call.side_effect = learning
    event, req = object(), object()
    await p.handle_memory_recall(event, req)
    assert order == ['memory', 'learning']
    p.modules.call.assert_awaited_once_with('inject_diversity_to_llm_request', event, req)


@pytest.mark.asyncio
async def test_unready_memory_still_reaches_learning_gate():
    p = plugin()
    p._ensure_plugin_ready.return_value = (False, 'waiting')
    await p.handle_memory_recall(object(), object())
    p.event_handler.handle_memory_recall.assert_not_awaited()
    p.modules.call.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_request_does_not_start_learning_work():
    p = plugin()
    p.event_handler.handle_memory_recall.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await p.handle_memory_recall(object(), object())
    p.modules.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_error_does_not_skip_independent_learning_hook():
    p = plugin()
    p.event_handler.handle_memory_recall.side_effect = RuntimeError('recall failed')
    with pytest.raises(RuntimeError, match='recall failed'):
        await p.handle_memory_recall(object(), object())
    p.modules.call.assert_awaited_once()


@pytest.mark.asyncio
async def test_normal_after_send_collects_learning_output():
    p = plugin()
    event = SimpleNamespace(get_extra=lambda *args: False)
    await p.handle_session_reset(event)
    p.modules.call.assert_awaited_once_with('on_bot_message_sent', event)
    p.event_handler.handle_session_reset.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_reset_does_not_collect_output():
    p = plugin()
    event = SimpleNamespace(get_extra=lambda *args: True)
    p.event_handler.handle_session_reset.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await p.handle_session_reset(event)
    p.modules.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_closes_after_learning_shutdown_error():
    p = plugin()
    p.modules.stop.side_effect = RuntimeError('learning stop failed')
    p._terminate_memory = AsyncMock()
    with pytest.raises(RuntimeError, match='learning stop failed'):
        await p.terminate()
    p._terminate_memory.assert_awaited_once()


def test_internal_binding_does_not_search_registry():
    def forbidden(*args): raise AssertionError('Unexpected registry lookup')
    context = SimpleNamespace(get_registered_star=forbidden, get_all_stars=forbidden)
    authority = object()
    binding = FeatureDelegation(SimpleNamespace(require_memory_authority=True), context, memory_authority=authority)
    assert binding.memory_plugin().star_cls is authority
    assert binding.should_delegate_memory()
