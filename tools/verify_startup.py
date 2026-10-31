"""Two cold starts from packaged ZIPs, empty databases and offline synthetic providers.
Set ASTRBOT_CORE to the directory containing astrbot/, MEMORY_FRAMEWORK_TEST_ROOT to a fresh test directory.
No production configuration or databases are read. Python dependencies must already be installed.
Adapted from the existing memory-v05 lifecycle regression, 2026-10-27.
"""
from pathlib import Path
import asyncio
import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
MODE = os.environ.get("MEMORY_FRAMEWORK_MODE", "memory_and_learning")
ROOT = Path(os.environ["MEMORY_FRAMEWORK_TEST_ROOT"]).resolve()
CORE = Path(os.environ["ASTRBOT_CORE"]).resolve()
RUNTIME = ROOT / "runtime"


def prepare():
    assert not RUNTIME.exists(), "Use a fresh validation directory"
    ROOT.mkdir(parents=True, exist_ok=True)
    (RUNTIME / "data").mkdir(parents=True)
    (RUNTIME / "empty-builtins").mkdir()
    sys.path.insert(0, str(PACKAGE_ROOT / "tools"))
    from install import install
    install(RUNTIME / "data", apply=True)
    (RUNTIME / "data/config").mkdir()
    (RUNTIME / "embedding-dim.json").write_text("4")
    # Let AstrBot load release schema defaults; only replace external-service
    # inputs and background learning for this fully synthetic offline test.
    lm = {"provider_settings": {"embedding_provider_id": "offline-embedding", "llm_provider_id": "offline-chat"},
          "reflection_engine": {"summary_trigger_rounds": 100}}
    sl = {"Self_Learning_Basic": {"enable_auto_learning": False, "enable_web_interface": False,
                                "enable_jargon_learning": False, "jargon_websearch_enabled": False},
          "Target_Settings": {"target_qq_list": ["group_424242"]}}
    lm.update(sl)
    lm['framework_modules'] = {'mode': MODE}
    for name, config in (("astrbot_plugin_livingmemory", lm),):
        (RUNTIME / f"data/config/{name}_config.json").write_text(json.dumps(config), "utf-8")


async def child(phase):
    os.environ["ASTRBOT_ROOT"] = str(RUNTIME)
    os.chdir(RUNTIME)
    sys.path[:0] = [str(RUNTIME), str(CORE)]
    if os.environ.get("MEMORY_FRAMEWORK_EXTRA_DEPS"):
        sys.path.insert(0, os.environ["MEMORY_FRAMEWORK_EXTRA_DEPS"])
    blocked = []
    connect = socket.socket.connect
    def offline_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in ("127.0.0.1", "::1", "localhost"):
            blocked.append("external connection")
            raise RuntimeError("External network disabled in startup validation")
        return connect(sock, address)
    socket.socket.connect = offline_connect
    from astrbot.core import db_helper, LogBroker, astrbot_config, pip_installer
    import astrbot.api  # Match the desktop entrypoint's bootstrap import order.
    from astrbot.core.provider.provider import Provider, EmbeddingProvider
    from astrbot.core.provider.entities import LLMResponse, ProviderMeta, ProviderType
    from astrbot.core.provider.manager import ProviderManager
    import astrbot.core.core_lifecycle as lifecycle
    import astrbot.core.star.star_manager as stars_module
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.message.components import Plain
    dim = json.loads((RUNTIME / "embedding-dim.json").read_text())

    class Chat(Provider):
        def __init__(self): super().__init__({"id": "offline-chat", "type": "offline"}, {})
        def get_current_key(self): return ""
        def set_key(self, key): pass
        async def get_models(self): return ["offline"]
        def meta(self): return ProviderMeta(id="offline-chat", model="offline", type="offline")
        async def text_chat(self, *args, **kwargs):
            return LLMResponse(role="assistant", completion_text='{"summary":"synthetic offline result"}')
        async def terminate(self): pass

    class Embedding(EmbeddingProvider):
        def __init__(self): super().__init__({"id": "offline-embedding", "type": "offline"}, {})
        def meta(self):
            return ProviderMeta(id="offline-embedding", model="offline", type="offline", provider_type=ProviderType.EMBEDDING)
        def get_dim(self): return dim
        async def get_embedding(self, text): return [1.0] + [0.0] * (dim - 1)
        async def get_embeddings(self, texts): return [await self.get_embedding(t) for t in texts]
        async def terminate(self): pass

    original_provider_init = ProviderManager.initialize
    async def providers(manager):
        await original_provider_init(manager)
        chat, embedding = Chat(), Embedding()
        manager.provider_insts.append(chat)
        manager.embedding_provider_insts.append(embedding)
        manager.inst_map.update({"offline-chat": chat, "offline-embedding": embedding})
        manager.curr_provider_inst = chat
        manager.default_chat_provider_id = "offline-chat"
    ProviderManager.initialize = providers
    original_plugin_init = stars_module.PluginManager.__init__
    def plugins(manager, *args, **kwargs):
        original_plugin_init(manager, *args, **kwargs)
        manager.reserved_plugin_path = str(RUNTIME / "empty-builtins")
    stars_module.PluginManager.__init__ = plugins
    # Dependency installation and external metadata refresh are outside this
    # offline lifecycle test. Both plugins use the installed portable libraries.
    stars_module._install_requirements_with_precheck = AsyncMock()
    pip_installer.install = AsyncMock(side_effect=RuntimeError("dependency installation disabled"))
    lifecycle.update_llm_metadata = AsyncMock()
    astrbot_config["platform"] = []
    astrbot_config["provider"] = []
    astrbot_config["provider_sources"] = []
    astrbot_config["log_level"] = "WARNING"
    core = lifecycle.AstrBotCoreLifecycle(LogBroker(), db_helper)
    learning_data = RUNTIME / 'data/plugin_data/astrbot_plugin_self_learning'
    learning_data_existed = learning_data.exists()
    started = None
    try:
        await asyncio.wait_for(core.initialize(), 100)
        started = asyncio.create_task(core.start())
        for _ in range(150):
            all_stars = core.star_context.get_all_stars()
            lm = next((s.star_cls for s in all_stars if "livingmemory" in (s.name or "").lower()), None)
            sl = lm.modules.learning if lm else None
            if lm and lm.initializer.is_initialized and (MODE == 'memory_only' or (sl and sl.db_manager.is_ready)):
                break
            await asyncio.sleep(0.2)
        else:
            raise AssertionError("plugins did not become ready: " + str(core.plugin_manager.failed_plugin_info))
        assert not core.plugin_manager.failed_plugin_dict
        await lm._ensure_runtime_components()
        assert len(core.star_context.get_all_stars()) == 1, 'Expected a single registered plugin'
        from astrbot.core.star.star_handler import star_handlers_registry
        handlers = star_handlers_registry.get_handlers_by_module_name(lm.__class__.__module__)
        owned = [h.handler_name for h in handlers]
        assert 'learning_status_command' in owned and 'on_learning_message' in owned
        assert not any('.learning.' in h.handler_module_path for h in star_handlers_registry)
        profile = {'registered_plugins': len(core.star_context.get_all_stars()),
                   'registered_handlers': len(owned),
                   'learning_imported_modules': sum('.learning.' in name for name in sys.modules),
                   'pending_async_tasks': len([t for t in asyncio.all_tasks() if not t.done()])}
        try:
            import psutil
            profile['process_rss_bytes'] = psutil.Process().memory_info().rss
        except ImportError:
            profile['process_rss_bytes'] = None
        if MODE == 'memory_only':
            assert sl is None and lm.modules.state == 'disabled'
            assert profile['learning_imported_modules'] == 0
            assert learning_data.exists() == learning_data_existed
            store, engine = lm.initializer.conversation_manager.store, lm.initializer.memory_engine
            await store.revoke_source('offline-bot:GroupMessage:424242', 'disabled-source')
            await store.dispatch_source_events(engine.consume_source_event)
            cursor = await store.connection.execute(
                "SELECT status FROM source_outbox WHERE source_uid='disabled-source'"
            )
            assert (await cursor.fetchone())['status'] == 'pending'
            assert not blocked
            result = dict(phase=phase, mode=MODE, core_started=True, single_plugin=True,
                          learning_not_imported=True, learning_data_not_created=not learning_data_existed,
                          disabled_source_delivery_pending=True, external_model_calls=0)
        else:
            assert sl.plugin_config.require_memory_authority and sl.plugin_config.enable_llm_hooks
            await lm._ensure_runtime_components()
            store, engine = lm.initializer.conversation_manager.store, lm.initializer.memory_engine
            if os.environ.get('MEMORY_FRAMEWORK_SWITCH_CHECK'):
                await engine.source_event_sink.reconcile()
                source_model = importlib.import_module(
                    lm.__class__.__module__.rsplit('.', 1)[0] + '.learning.models.orm.source_lifecycle'
                ).LearningSource
                async with sl.db_manager.engine.get_session() as session:
                    record = await session.get(source_model, ('offline-bot:GroupMessage:424242', 'disabled-source'))
                    assert record is not None and record.state == 'revoked'
            scope = "offline-bot:GroupMessage:424242"
            cursor = await store.connection.execute("SELECT COUNT(*) FROM messages WHERE session_id!=?", (scope,))
            original_messages = (await cursor.fetchone())[0]
            assert original_messages == 0
            assert sl.plugin_config.db_type == "sqlite"
            assert not sl.plugin_config.auto_apply_persona_updates
            assert not sl.plugin_config.auto_apply_approved_persona
            package = importlib.import_module(lm.__class__.__module__.rsplit(".", 1)[0] + ".core.source_identity")
            uid = package.fingerprint([scope, "user", "9001"])
            source_events = importlib.import_module(lm.__class__.__module__.rsplit(".", 1)[0] + ".core.source_events")
            evidence_reader = importlib.import_module(lm.__class__.__module__.rsplit(".", 1)[0] + ".core.tools.memory_evidence_reader")
            def event(recall=False):
                m = AstrBotMessage()
                m.type, m.self_id, m.message_id, m.group_id = MessageType.GROUP_MESSAGE, "999", "9001", "424242"
                m.sender = MessageMember(user_id="alice", nickname="Alice")
                m.message = [Plain("Alice drinks tea")]
                m.message_str = "Alice drinks tea"
                m.raw_message = ({"post_type": "notice", "notice_type": "group_recall", "message_id": "9001"} if recall else {})
                return AstrMessageEvent(m.message_str, m, PlatformMetadata("aiocqhttp", "offline", "offline-bot"), "424242")
            if phase == 1:
                ev = event()
                await lm._run_passive_group_capture(ev)
                await sl.on_message(ev)
                for _ in range(40):
                    raw = await sl.db_manager.get_unprocessed_messages(group_id="424242")
                    if raw:
                        break
                    await asyncio.sleep(0.1)
                assert raw, "real self_learning capture did not save synthetic input"
                messages = await store.get_messages(scope)
                assert len(messages) == 1
                persona = messages[0].metadata["reflection_context"]["persona_id"]
                mid = await engine.add_memory(content="Alice drinks tea", session_id=scope, persona_id=persona,
                                              source_messages=[m.to_dict() for m in messages])
                candidates = await lm.find_evidence_candidates(ev, "tea")
                assert any(c["memory_id"] == mid for c in candidates)
                assert await lm.govern_external_context(ev, candidates)
                assert await source_events.apply_source_recall(event(True), engine, store, lm.initializer.conversation_manager)
                assert not await sl.db_manager.get_unprocessed_messages(group_id="424242")
                assert not await engine.memory_sources_current(mid)
                result = await evidence_reader.read_memory_evidence_core(memory_engine=engine,
                    recall_session_id=scope, recall_persona_id=persona, memory_id=mid, include_source=True)
                assert result["status"] != "success"
                (RUNTIME / "scenario.json").write_text(json.dumps({"mid": mid, "persona": persona}))
            else:
                state = json.loads((RUNTIME / "scenario.json").read_text())
                await engine.source_event_sink.reconcile()
                assert not await engine.memory_sources_current(state["mid"])
                evidence = await evidence_reader.read_memory_evidence_core(memory_engine=engine,
                    recall_session_id=scope, recall_persona_id=state["persona"], memory_id=state["mid"],
                    include_source=True)
                assert evidence["status"] != "success"
                assert not await sl.db_manager.get_unprocessed_messages(group_id="424242")
                # The replay guard survives a complete process stop and cold start.
                await lm._run_passive_group_capture(event())
                await sl.on_message(event())
                await asyncio.sleep(0.3)
                assert await store.get_message_count(scope) == 0
                assert not await sl.db_manager.get_unprocessed_messages(group_id="424242")
            assert not blocked, "unexpected external connection attempt"
            result = dict(phase=phase, core_started=True, plugins_ready=True, external_model_calls=0,
                          original_messages=original_messages, synthetic_capture_and_recall=phase == 1, replay_blocked=phase == 2)
        result['runtime_profile'] = profile
    finally:
        if hasattr(core, "curr_tasks"):
            await asyncio.wait_for(core.stop(), 45)
        if started:
            await asyncio.wait_for(started, 5)
    result["stopped_cleanly"] = True
    (ROOT / f"startup-phase-{phase}.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    if "--child" in sys.argv:
        asyncio.run(child(int(sys.argv[-1])))
    else:
        if "--resume" not in sys.argv:
            prepare()
        for phase in (1, 2):
            with (ROOT / f"startup-phase-{phase}.log").open("w", encoding="utf-8") as log:
                run = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--child", str(phase)],
                                     stdout=log, stderr=subprocess.STDOUT, timeout=200,
                                     env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            assert run.returncode == 0, f"startup phase {phase} failed; inspect its local log"
        results = [json.loads((ROOT / f"startup-phase-{p}.json").read_text()) for p in (1, 2)]
        (ROOT / "startup-results.json").write_text(json.dumps(results, indent=2))
        print(json.dumps(results))
