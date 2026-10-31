"""Resume persisted summary work without reconstructing or replaying chat events."""
import asyncio
import json
import time
from types import SimpleNamespace

from astrbot.api import logger
from .passive_group_capture import is_session_enabled, is_plugin_enabled_for_session
from .memory_scope import is_event_memory_allowed
from .summary_window import capture_window


class SummaryRecovery:
    def __init__(self, reflection):
        self.reflection = reflection
        self.store = reflection.conversation_manager.store
        self.task = None

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._loop())

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def prepare(self, session_id, messages, persona_id, start, end, retries,
                      memory_scope, expected_revision):
        messages, persona_id, memory_scope = capture_window(messages, persona_id, memory_scope)
        if not messages:
            return asyncio.sleep(0)
        end = start + len(messages)
        job_id = await self.store.enqueue_summary(
            session_id, messages, persona_id, start, end, memory_scope, expected_revision
        )
        return self.run_job(job_id)

    async def run_job(self, job_id):
        async with self.store.connection.execute("SELECT * FROM summary_jobs WHERE id=?", (job_id,)) as cursor:
            row = await cursor.fetchone()
        if row is None or row["status"] != "pending":
            return
        payload = json.loads(row["payload"])
        session = row["session_id"]
        manager = self.reflection.conversation_manager
        revision = manager.summary_window_revision(session, payload)
        if not await manager.summary_revision_current(session, revision):
            await self.store.mark_summary_job(job_id, "cancelled", error="source_window_changed")
            return
        messages = await self.store.summary_job_messages(payload)
        if not messages:
            await self.store.mark_summary_job(job_id, "cancelled", error="source_revoked_or_missing")
            return
        if not self.messages_allowed(messages):
            return  # Keep pending; a temporary policy change is not data loss.
        await self.store.mark_summary_job(job_id, "running")
        try:
            await self.reflection._storage_task(
                session, messages, payload["persona_id"], payload["start"], payload["end"],
                row["attempts"], memory_scope=payload["scope"], expected_revision=revision,
            )
            cursor = await self.reflection.conversation_manager.get_session_metadata(
                session, "last_summarized_index", 0
            )
            current = await self.store.sources_current(payload["sources"])
            current = current and await manager.summary_revision_current(session, revision)
            status = "cancelled" if not current else ("completed" if cursor >= payload["end"] else "pending")
            await self.store.mark_summary_job(job_id, status)
        except asyncio.CancelledError:
            await asyncio.shield(self.store.mark_summary_job(job_id, "pending", error="interrupted"))
            raise
        except Exception as exc:
            await self.store.mark_summary_job(job_id, "pending", error=type(exc).__name__)
            raise

    def messages_allowed(self, messages):
        for message in messages:
            if message.role != "user":
                continue
            event = SimpleNamespace(
                unified_msg_origin=message.session_id, sender_id=message.sender_id,
                group_id=message.group_id or "", platform=message.platform or "",
            )
            if not is_event_memory_allowed(self.reflection.config_manager, event):
                return False
        return True

    async def plan_dirty_sessions(self):
        async with self.store.connection.execute(
            "SELECT * FROM reflection_dirty WHERE blocked_job_id IS NULL AND next_attempt_at<=? "
            "ORDER BY updated_at LIMIT 20", (time.time(),)
        ) as cursor:
            dirty = await cursor.fetchall()
        for row in dirty:
            session = row["session_id"]
            # Rotate quiet/disabled sessions too, so a fixed first page cannot
            # starve other dirty sessions. A concurrent insert wins by generation.
            await self.store.rotate_dirty_session(session, row["generation"])
            if not await is_session_enabled(session) or not await is_plugin_enabled_for_session(session):
                continue
            if session in self.reflection._storage_sessions_inflight:
                continue
            manager = self.reflection.conversation_manager
            start = int(await manager.get_session_metadata(session, "last_summarized_index", 0) or 0)
            count = await self.store.get_message_count(session)
            if count <= start:
                await self.store.rotate_dirty_session(session, row["generation"], completed=True)
                continue
            threshold = max(1, int(self.reflection.config_manager.get("reflection_engine.summary_trigger_rounds", 10)))
            if count - start < threshold * 2:
                continue
            revision = await manager.get_session_revision(session)
            messages = await manager.get_messages_range(session, start, min(count, start + 200))
            if not messages:
                continue
            unresolved = [m for m in messages if m.metadata.get("reflection_context_status") == "unresolved"]
            if unresolved:
                resolver = getattr(manager, "reflection_context_resolver", None)
                try:
                    if resolver is None:
                        raise RuntimeError("context resolver unavailable")
                    for message in unresolved:
                        event = SimpleNamespace(
                            unified_msg_origin=message.session_id,
                            get_sender_id=lambda m=message: m.sender_id,
                            get_platform_name=lambda m=message: m.platform or "",
                            get_group_id=lambda m=message: m.group_id or "",
                        )
                        context = await asyncio.wait_for(resolver(event), 2.0)
                        await self.store.assign_reflection_context(message, context)
                except Exception:
                    await self.store.defer_context_resolution(session, row["generation"])
                    continue
                await manager.invalidate_cache(session)
            if not self.messages_allowed(messages):
                continue
            context = messages[0].metadata.get("reflection_context") or json.loads(row["context_json"])
            messages, persona, scope = capture_window(
                messages, context.get("persona_id"), context.get("memory_scope")
            )
            split = self.reflection._find_window_split_boundary(
                messages,
                split_by_day=bool(self.reflection.config_manager.get("reflection_engine.window_split_by_day", True)),
                gap_minutes=int(self.reflection.config_manager.get("reflection_engine.window_split_gap_minutes", 180)),
            )
            if split:
                messages = messages[:split[0]]
            if not messages or not persona or not scope:
                continue
            await self.store.enqueue_summary(
                session, messages, persona, start, start + len(messages), scope, revision,
            )

    async def recover_once(self):
        await self.plan_dirty_sessions()
        for job in await self.store.pending_summary_jobs():
            session = job["session_id"]
            if not await is_session_enabled(session) or not await is_plugin_enabled_for_session(session):
                continue
            async with self.reflection._storage_state_lock:
                if session in self.reflection._storage_sessions_inflight:
                    continue
                self.reflection._storage_sessions_inflight.add(session)
            try:
                await self.run_job(job["id"])
            finally:
                self.reflection._storage_sessions_inflight.discard(session)

    async def _loop(self):
        while True:
            try:
                await self.recover_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("持久总结任务保留待重试", exc_info=True)
            await asyncio.sleep(15)
