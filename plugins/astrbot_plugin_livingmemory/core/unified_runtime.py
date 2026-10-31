"""Own the optional learning module without a second Star or registry discovery."""
from __future__ import annotations

import asyncio
from pathlib import Path


class ModuleRuntime:
    """Fixed mode per plugin load; unloading drains the existing learning lifecycle.

    Memory is always the authority. A disabled/unready learning module never
    acknowledges source events: the durable outbox and ledger reconcile on load.
    """

    def __init__(self, memory, context, config, *, learning_factory=None):
        self.memory = memory
        self.context = context
        self.config = config
        self.mode = config.get('framework_modules', {}).get('mode', 'memory_and_learning')
        if self.mode not in ('memory_only', 'memory_and_learning'):
            raise ValueError('Unknown framework_modules.mode: ' + str(self.mode))
        self.learning = None
        self.state = 'disabled' if self.mode == 'memory_only' else 'created'
        self._factory = learning_factory
        self._lock = asyncio.Lock()

    @staticmethod
    def check_legacy_conflict():
        legacy = Path(__file__).resolve().parents[2] / 'astrbot_plugin_self_learning'
        if legacy.exists():
            raise RuntimeError(
                'Move the old astrbot_plugin_self_learning source directory out of data/plugins '
                'before enabling the unified framework. Preserve its configuration and plugin_data.'
            )

    async def start(self):
        async with self._lock:
            if self.state in ('disabled', 'ready'):
                return
            if self.state != 'created':
                raise RuntimeError('Reload the plugin before restarting a stopped/failed learning module')
            self.check_legacy_conflict()
            self.state = 'starting'
            try:
                factory = self._factory
                if factory is None:
                    # Do not import the learning dependency tree in memory-only mode.
                    from ..learning.module import LearningModule
                    factory = LearningModule
                self.learning = factory(self.context, self.config, memory_authority=self.memory)
                await self.learning.initialize()
            except BaseException:
                self.state = 'failed'
                # LearningModule owns rollback if its initialize fails.
                self.learning = None
                raise
            self.state = 'ready'

    def source_consumer(self):
        if self.state != 'ready' or self.learning is None:
            raise RuntimeError('Internal learning source consumer is not ready; delivery remains pending')
        return self.learning, self.learning.on_memory_source_event

    async def call(self, method, *args):
        if self.state == 'ready' and self.learning is not None:
            return await getattr(self.learning, method)(*args)

    async def command(self, method, event):
        if self.state != 'ready' or self.learning is None:
            yield event.plain_result('学习模块未启用或未就绪；请检查 framework_modules.mode 和启动日志。')
            return
        async for result in getattr(self.learning, method)(event):
            yield result

    async def stop(self):
        async with self._lock:
            if self.learning is None:
                return
            self.state = 'stopping'
            try:
                await self.learning.terminate()
            finally:
                self.learning = None
                self.state = 'stopped'

    def status(self):
        return {'mode': self.mode, 'learning_state': self.state, 'memory_authority': 'internal'}
