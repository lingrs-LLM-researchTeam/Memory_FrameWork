from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from self_learning_EterU.config import PluginConfig
from self_learning_EterU.core.feature_delegation import FeatureDelegation
from self_learning_EterU.services.hooks.llm_hook_handler import LLMHookHandler


def test_authority_mode_prevents_automatic_persona_rewrites():
    config = PluginConfig(require_memory_authority=True, auto_apply_persona_updates=True,
                          auto_apply_approved_persona=True, use_persona_manager_updates=True)
    assert not config.auto_apply_persona_updates
    assert not config.auto_apply_approved_persona
    assert not config.use_persona_manager_updates
    config.auto_apply_persona_updates = True
    assert not config.auto_apply_persona_updates
    delegation = FeatureDelegation(config, SimpleNamespace(get_all_stars=lambda: []))
    assert delegation.should_delegate_memory()


def handler(delegation):
    return LLMHookHandler(
        plugin_config=PluginConfig(require_memory_authority=True, enable_llm_hooks=True),
        diversity_manager=Mock(), social_context_injector=Mock(), v2_integration=Mock(),
        jargon_query_service=Mock(), temporary_persona_updater=Mock(), perf_tracker=Mock(),
        group_id_to_unified_origin={}, feature_delegation=delegation,
    )


@pytest.mark.asyncio
async def test_authority_unavailable_does_not_fall_back_to_unverified_profiles():
    service = handler(SimpleNamespace(memory_plugin=lambda: None))
    req = SimpleNamespace(prompt="hello", system_prompt="fixed", extra_user_content_parts=[])
    await service.handle(SimpleNamespace(), req)
    assert req.system_prompt == "fixed"
    assert req.extra_user_content_parts == []
    service._social_context_injector.format_complete_context.assert_not_called()
    service._v2_integration.get_enhanced_context.assert_not_called()


@pytest.mark.asyncio
async def test_authority_reconstructs_evidence_before_injection():
    gate = AsyncMock(return_value=["quoted and verified provenance"])
    service = handler(SimpleNamespace(memory_plugin=lambda: SimpleNamespace(
        star_cls=SimpleNamespace(govern_external_context=gate,
                                 find_evidence_candidates=AsyncMock(return_value=[{"memory_id": 7}])))))
    event = SimpleNamespace(get_extra=lambda key, default: [{"memory_id": 7, "text": "forged"}])
    req = SimpleNamespace(prompt="hello", system_prompt="fixed", extra_user_content_parts=[])
    service._inject = Mock()
    await service.handle(event, req)
    gate.assert_awaited_once()
    assert service._inject.call_args.args[1] == ["quoted and verified provenance"]
    service._social_context_injector.format_complete_context.assert_not_called()
