"""Durable extraction work; raw inputs and dirty-session jobs commit together."""
import json
import time

from ..core.memory_source import source_reference
from ..core.source_identity import fingerprint


class SummaryJobsMixin:
    async def commit_summary_metadata(self, window, key, value):
        """Validate sources/positions and commit progress in the raw write lock."""
        if key not in {"last_summarized_index", "pending_summary"}:
            raise ValueError("not summary metadata")
        async with self._write_lock:
            if not await window.is_current(self):
                return False
            cursor = await self.connection.execute(
                "SELECT metadata FROM sessions WHERE session_id=?", (window.session_id,))
            row = await cursor.fetchone()
            if row is None:
                return False
            metadata = json.loads(row["metadata"] or "{}")
            if key == "last_summarized_index":
                metadata[key] = max(int(metadata.get(key, 0) or 0), int(value))
                metadata["pending_summary"] = None
            else:
                metadata[key] = value
            await self.connection.execute("UPDATE sessions SET metadata=? WHERE session_id=?",
                                          (json.dumps(metadata), window.session_id))
            await self.connection.commit()
            return True

    async def initialize_summary_jobs(self):
        await self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS reflection_dirty(
                session_id TEXT PRIMARY KEY,context_json TEXT NOT NULL,
                generation INTEGER NOT NULL DEFAULT 1,updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS summary_jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,job_key TEXT UNIQUE NOT NULL,
                session_id TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,updated_at REAL NOT NULL,error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_summary_jobs_pending ON summary_jobs(status,id);
        """)
        columns = {r[1] for r in await (await self.connection.execute("PRAGMA table_info(reflection_dirty)")).fetchall()}
        for name, declaration in (("blocked_job_id", "INTEGER"), ("next_attempt_at", "REAL NOT NULL DEFAULT 0")):
            if name not in columns:
                await self.connection.execute(f"ALTER TABLE reflection_dirty ADD COLUMN {name} {declaration}")
        # One database is owned by one deployment/worker. Interrupted attempts
        # are retried from persisted source IDs, never from an in-memory task.
        await self.connection.execute("UPDATE summary_jobs SET status='pending' WHERE status='running'")
        await self.connection.commit()

    async def record_reflection_input(self, message):
        context = (message.metadata or {}).get("reflection_context")
        unresolved = (message.metadata or {}).get("reflection_context_status") == "unresolved"
        if not unresolved and (not isinstance(context, dict) or not context.get("persona_id") or not context.get("memory_scope")):
            return
        await self.connection.execute(
            "INSERT INTO reflection_dirty(session_id,context_json,generation,updated_at) VALUES(?,?,1,?) "
            "ON CONFLICT(session_id) DO UPDATE SET context_json=excluded.context_json,"
            "generation=generation+1,blocked_job_id=NULL,next_attempt_at=0,updated_at=excluded.updated_at",
            (message.session_id, json.dumps(context or {}, ensure_ascii=False), time.time()),
        )

    async def assign_reflection_context(self, message, context):
        if not isinstance(context, dict) or not context.get("persona_id") or not context.get("memory_scope"):
            raise ValueError("capture context is unresolved")
        async with self._write_lock:
            cursor = await self.connection.execute(
                "SELECT metadata FROM messages WHERE id=? AND session_id=?", (message.id, message.session_id),
            )
            row = await cursor.fetchone()
            if row is None:
                return False
            metadata = json.loads(row["metadata"] or "{}")
            if metadata.get("reflection_context_status") != "unresolved":
                message.metadata = metadata
                return True
            metadata.update(reflection_context=context, reflection_context_status="resolved")
            await self.connection.execute("UPDATE messages SET metadata=? WHERE id=?",
                                          (json.dumps(metadata), message.id))
            message.metadata = metadata
            await self.record_reflection_input(message)
            await self.connection.commit()
            return True

    async def enqueue_summary(self, session_id, messages, persona_id, start, end, scope, revision):
        references = [source_reference(m.to_dict()) for m in messages]
        payload = dict(persona_id=persona_id, start=start, end=end, scope=scope,
                       revision=revision, sources=references)
        key = fingerprint(["summary-v3", session_id, payload])
        async with self._write_lock:
            cursor = await self.connection.execute("SELECT id,status FROM summary_jobs WHERE job_key=?", (key,))
            existing = await cursor.fetchone()
            if existing:
                if existing["status"] in {"failed", "cancelled"}:
                    await self.connection.execute(
                        "UPDATE reflection_dirty SET blocked_job_id=? WHERE session_id=?",
                        (existing["id"], session_id),
                    )
                    await self.connection.commit()
                return int(existing[0])
            cursor = await self.connection.execute("SELECT generation FROM reflection_dirty WHERE session_id=?", (session_id,))
            dirty = await cursor.fetchone()
            # Generation is bookkeeping, not part of the source-window retry key.
            payload["generation"] = dirty[0] if dirty else None
            cursor = await self.connection.execute(
                "SELECT COUNT(*) FROM summary_jobs WHERE status IN ('pending','running')"
            )
            if (await cursor.fetchone())[0] >= 500:
                raise RuntimeError("summary queue full; raw inputs remain dirty for later planning")
            await self.connection.execute(
                "INSERT OR IGNORE INTO summary_jobs(job_key,session_id,payload,updated_at) VALUES(?,?,?,?)",
                (key, session_id, json.dumps(payload, ensure_ascii=False), time.time()),
            )
            await self.connection.commit()
            cursor = await self.connection.execute("SELECT id FROM summary_jobs WHERE job_key=?", (key,))
            return int((await cursor.fetchone())[0])

    async def rotate_dirty_session(self, session_id, generation, *, completed=False):
        async with self._write_lock:
            if completed:
                await self.connection.execute(
                    "DELETE FROM reflection_dirty WHERE session_id=? AND generation=?",
                    (session_id, generation),
                )
            else:
                await self.connection.execute(
                    "UPDATE reflection_dirty SET updated_at=? WHERE session_id=? AND generation=?",
                    (time.time(), session_id, generation),
                )
            await self.connection.commit()

    async def pending_summary_jobs(self):
        async with self.connection.execute(
            "SELECT * FROM summary_jobs WHERE status='pending' AND attempts<3 ORDER BY id LIMIT 20"
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def mark_summary_job(self, job_id, status, *, error=None):
        async with self._write_lock:
            await self.connection.execute(
                "UPDATE summary_jobs SET status=?,error=?,updated_at=?,"
                "attempts=attempts+CASE WHEN ?='running' THEN 1 ELSE 0 END WHERE id=? "
                "AND status NOT IN ('completed','cancelled')",
                (status, error, time.time(), status, job_id),
            )
            await self.connection.execute(
                "UPDATE summary_jobs SET status='failed' WHERE id=? AND status='pending' AND attempts>=3",
                (job_id,),
            )
            await self.connection.execute(
                "UPDATE reflection_dirty SET blocked_job_id=? WHERE session_id="
                "(SELECT session_id FROM summary_jobs WHERE id=? AND status='failed') "
                "AND generation=(SELECT json_extract(payload,'$.generation') FROM summary_jobs WHERE id=?)",
                (job_id, job_id, job_id),
            )
            await self.connection.commit()

    async def retry_summary_job(self, session_id, job_id):
        async with self._write_lock:
            cursor = await self.connection.execute(
                "UPDATE summary_jobs SET status='pending',attempts=0,error=NULL,updated_at=? "
                "WHERE id=? AND session_id=? AND status='failed'", (time.time(), job_id, session_id),
            )
            if cursor.rowcount:
                await self.connection.execute(
                    "UPDATE reflection_dirty SET blocked_job_id=NULL,next_attempt_at=0 "
                    "WHERE session_id=? AND blocked_job_id=?", (session_id, job_id),
                )
            await self.connection.commit()
            return cursor.rowcount > 0

    async def defer_context_resolution(self, session_id, generation):
        async with self._write_lock:
            await self.connection.execute(
                "UPDATE reflection_dirty SET next_attempt_at=? WHERE session_id=? AND generation=?",
                (time.time() + 30, session_id, generation),
            )
            await self.connection.commit()

    async def summary_job_messages(self, payload):
        from ..core.models.conversation_models import Message
        if not await self.sources_current(payload["sources"]):
            return None
        messages = []
        for ref in payload["sources"]:
            async with self.connection.execute(
                "SELECT m.* FROM messages m JOIN source_ledger l ON l.message_id=m.id"
                " WHERE l.session_id=? AND l.source_uid=?", (ref["session_id"], ref["source_uid"]),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None
            data = dict(row)
            data["metadata"] = json.loads(data.get("metadata") or "{}")
            data.pop("source_uid", None)
            messages.append(Message.from_dict(data))
        return messages
