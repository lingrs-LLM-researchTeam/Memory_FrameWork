"""Lifecycle/source-delivery contracts; no installed AstrBot or external network."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


core = ROOT / 'plugins/astrbot_plugin_livingmemory/core'
Runtime = load('runtime_under_test', core / 'unified_runtime.py').ModuleRuntime
Delivery = load('delivery_under_test', core / 'source_companions.py').SourceCompanionDelivery
merge = load('migration_under_test', ROOT / 'tools/migrate_config.py').merge_configs


class ModuleTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, *, mode='memory_and_learning', factory=None):
        return Runtime(object(), object(), {'framework_modules': {'mode': mode}}, learning_factory=factory)

    async def test_disabled_allocates_no_module_and_does_not_acknowledge(self):
        def forbidden(*args, **kwargs):
            self.fail('Disabled module was constructed')
        runtime = self.runtime(mode='memory_only', factory=forbidden)
        await runtime.start()
        await runtime.call('on_message', object())
        with self.assertRaisesRegex(RuntimeError, 'pending'):
            runtime.source_consumer()
        self.assertIsNone(runtime.learning)

    async def test_shared_authority_one_initialization_and_orderly_stop(self):
        module = SimpleNamespace(initialize=AsyncMock(), terminate=AsyncMock(),
                                 on_message=AsyncMock(), on_memory_source_event=AsyncMock())
        captured = []
        def factory(context, config, *, memory_authority):
            captured.append(memory_authority)
            return module
        runtime = self.runtime(factory=factory)
        await asyncio.gather(runtime.start(), runtime.start())
        self.assertEqual(captured, [runtime.memory])
        module.initialize.assert_awaited_once()
        event = object()
        await runtime.call('on_message', event)
        module.on_message.assert_awaited_once_with(event)
        self.assertIs(runtime.source_consumer()[0], module)
        await runtime.stop()
        await runtime.stop()
        module.terminate.assert_awaited_once()
        self.assertEqual(runtime.state, 'stopped')
        await runtime.call('on_message', object())
        module.on_message.assert_awaited_once()

    async def test_failed_initialization_cannot_acknowledge(self):
        module = SimpleNamespace(initialize=AsyncMock(side_effect=RuntimeError('db failure')))
        runtime = self.runtime(factory=lambda *args, **kwargs: module)
        with self.assertRaisesRegex(RuntimeError, 'db failure'):
            await runtime.start()
        with self.assertRaises(RuntimeError):
            runtime.source_consumer()
        self.assertEqual(runtime.state, 'failed')

    async def test_direct_delivery_replays_ledger_and_propagates_failure(self):
        revoked = {'sequence': 1, 'session_id': 'p:GroupMessage:g', 'source_uid': 'm', 'revision': 1, 'state': 'revoked'}
        cursor = SimpleNamespace(fetchall=AsyncMock(side_effect=[[revoked], [], []]))
        store = SimpleNamespace(connection=SimpleNamespace(execute=AsyncMock(return_value=cursor)))
        handler = AsyncMock()
        instance = object()
        delivery = Delivery(object(), store, consumer_provider=lambda: (instance, handler))
        current = {**revoked, 'source_uid': 'n'}
        await delivery(current)
        self.assertEqual(handler.await_args_list[0].args[0], revoked)
        self.assertEqual(handler.await_args_list[1].args[0], current)
        handler.side_effect = RuntimeError('consumer unavailable')
        with self.assertRaisesRegex(RuntimeError, 'unavailable'):
            await delivery(current)

    async def test_failed_reconciliation_retries_and_replacement_replays(self):
        row = {'sequence': 1, 'session_id': 's', 'source_uid': 'm', 'revision': 1, 'state': 'active'}
        cursor = SimpleNamespace(fetchall=AsyncMock(side_effect=[[row], [row], [], [row], []]))
        store = SimpleNamespace(connection=SimpleNamespace(execute=AsyncMock(return_value=cursor)))
        handler = AsyncMock(side_effect=RuntimeError('retry'))
        consumer = [object(), handler]
        delivery = Delivery(object(), store, consumer_provider=lambda: tuple(consumer))
        with self.assertRaises(RuntimeError):
            await delivery.reconcile()
        handler.side_effect = None
        await delivery.reconcile()
        self.assertEqual(handler.await_args.args[0]['state'], 'revised')
        calls = handler.await_count
        await delivery.reconcile()
        self.assertEqual(handler.await_count, calls)
        consumer[0] = object()
        await delivery.reconcile()
        self.assertEqual(handler.await_count, calls + 1)


class ConfigTests(unittest.TestCase):
    def test_merge_preserves_providers_paths_and_weights(self):
        memory = {'provider_settings': {'llm_provider_id': 'chat'}, 'weights': {'value': .75}}
        learning = {'Storage_Settings': {'data_dir': '/existing'}, 'Reinforcement': {'reward': .25}}
        result = merge(memory, learning)
        self.assertEqual(result['weights'], memory['weights'])
        self.assertEqual(result['Reinforcement'], learning['Reinforcement'])
        self.assertEqual(result['Storage_Settings']['data_dir'], '/existing')
        self.assertNotIn('framework_modules', memory)
        self.assertNotIn('provider_settings', learning)

    def test_conflicts_fail_without_leaking_values(self):
        with self.assertRaises(ValueError) as caught:
            merge({'key': 'secret-a'}, {'key': 'secret-b'})
        self.assertNotIn('secret', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
