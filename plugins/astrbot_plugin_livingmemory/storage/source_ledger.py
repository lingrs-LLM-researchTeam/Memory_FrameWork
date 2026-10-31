"""Authoritative source revisions and an outbox in the raw-message transaction."""
import asyncio
import json
import time

from ..core.memory_source import source_reference


class SourceLedgerMixin:
    async def prune_source_window(self, session_id, count):
        """Storage retention keeps the authoritative snapshot revision valid."""
        try:
            async with self.connection.execute(
                "SELECT * FROM messages WHERE session_id=? ORDER BY timestamp,id LIMIT ?", (session_id, count)
            ) as cursor:
                rows = await cursor.fetchall()
            for row in rows:
                ref = source_reference(dict(row))
                await self.connection.execute(
                    "UPDATE source_ledger SET content_hash=?,platform_message_id=? "
                    "WHERE session_id=? AND source_uid=?",
                    (ref["content_hash"], ref["platform_message_id"], session_id, ref["source_uid"]),
                )
            await self.connection.execute("UPDATE source_retention_control SET pruning=1 WHERE id=1")
            cursor = await self.connection.execute(
                "DELETE FROM messages WHERE id IN (SELECT id FROM messages WHERE session_id=?"
                " ORDER BY timestamp,id LIMIT ?)", (session_id, count),
            )
            await self.connection.execute("UPDATE source_retention_control SET pruning=0 WHERE id=1")
            return max(0, cursor.rowcount)
        except BaseException:
            await self.connection.rollback()
            raise

    async def initialize_source_ledger(self):
        db = self.connection
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS source_ledger(
                session_id TEXT NOT NULL, source_uid TEXT NOT NULL,
                message_id INTEGER, revision INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL, sender_id TEXT, role TEXT,
                updated_at REAL NOT NULL, content_hash TEXT, platform_message_id TEXT,
                PRIMARY KEY(session_id, source_uid)
            );
            CREATE TABLE IF NOT EXISTS source_outbox(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL, source_uid TEXT NOT NULL,
                revision INTEGER NOT NULL, state TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, error TEXT,
                UNIQUE(session_id, source_uid, revision, state)
            );
            CREATE INDEX IF NOT EXISTS idx_source_outbox_pending ON source_outbox(status, id);
            CREATE TABLE IF NOT EXISTS source_retention_control(
                id INTEGER PRIMARY KEY CHECK(id=1), pruning INTEGER NOT NULL DEFAULT 0
            );
            INSERT OR IGNORE INTO source_retention_control(id) VALUES(1);
            CREATE TRIGGER IF NOT EXISTS source_replay_guard BEFORE INSERT ON messages
            WHEN NEW.source_uid IS NOT NULL AND EXISTS(
                SELECT 1 FROM source_ledger WHERE session_id=NEW.session_id
                    AND source_uid=NEW.source_uid AND state!='active'
            ) BEGIN
                SELECT RAISE(ABORT, 'revoked source cannot be replayed');
            END;
        """)
        columns = {row[1] for row in await (await db.execute("PRAGMA table_info(source_ledger)")).fetchall()}
        if "platform_message_id" not in columns:
            await db.execute("ALTER TABLE source_ledger ADD COLUMN platform_message_id TEXT")
        columns = {row[1] for row in await (await db.execute("PRAGMA table_info(source_outbox)")).fetchall()}
        if "next_attempt_at" not in columns:
            await db.execute("ALTER TABLE source_outbox ADD COLUMN next_attempt_at REAL NOT NULL DEFAULT 0")
        # Replace the v0.3 triggers when upgrading an existing database.
        await db.executescript("""
            DROP TRIGGER IF EXISTS source_insert;
            DROP TRIGGER IF EXISTS source_delete;
            CREATE TRIGGER source_insert AFTER INSERT ON messages BEGIN
                UPDATE messages SET source_uid=COALESCE(NEW.source_uid,
                    'local:'||NEW.session_id||':'||NEW.id),
                    metadata=json_set(COALESCE(NEW.metadata,'{}'),
                        '$.source_uid', COALESCE(NEW.source_uid,'local:'||NEW.session_id||':'||NEW.id),
                        '$.revision', COALESCE(json_extract(NEW.metadata,'$.revision'),0))
                    WHERE id=NEW.id;
                INSERT INTO source_ledger(session_id,source_uid,message_id,revision,state,sender_id,role,updated_at,platform_message_id)
                    VALUES(NEW.session_id,COALESCE(NEW.source_uid,'local:'||NEW.session_id||':'||NEW.id),
                        NEW.id,COALESCE(json_extract(NEW.metadata,'$.revision'),0),
                        'active',NEW.sender_id,NEW.role,unixepoch('now'),
                        json_extract(NEW.metadata,'$.platform_message_id'));
            END;
            CREATE TRIGGER IF NOT EXISTS source_delete AFTER DELETE ON messages
            WHEN (SELECT pruning FROM source_retention_control WHERE id=1)=0 BEGIN
                UPDATE source_ledger SET state='revoked',revision=revision+1,updated_at=unixepoch('now')
                    WHERE session_id=OLD.session_id
                    AND source_uid=COALESCE(OLD.source_uid,'local:'||OLD.session_id||':'||OLD.id)
                    AND state!='revoked';
                INSERT OR IGNORE INTO source_outbox(session_id,source_uid,revision,state,created_at)
                    SELECT session_id,source_uid,revision,state,unixepoch('now') FROM source_ledger
                    WHERE session_id=OLD.session_id
                    AND source_uid=COALESCE(OLD.source_uid,'local:'||OLD.session_id||':'||OLD.id);
            END;
            CREATE TRIGGER IF NOT EXISTS source_revision AFTER UPDATE OF content,sender_id,role ON messages
            WHEN NEW.content!=OLD.content OR NEW.sender_id!=OLD.sender_id OR NEW.role!=OLD.role BEGIN
                UPDATE source_ledger SET revision=revision+1,sender_id=NEW.sender_id,role=NEW.role,
                    updated_at=unixepoch('now') WHERE session_id=OLD.session_id
                    AND source_uid=COALESCE(OLD.source_uid,'local:'||OLD.session_id||':'||OLD.id);
                UPDATE messages SET metadata=json_set(COALESCE(NEW.metadata,'{}'),'$.revision',
                    (SELECT revision FROM source_ledger WHERE session_id=OLD.session_id
                    AND source_uid=COALESCE(OLD.source_uid,'local:'||OLD.session_id||':'||OLD.id)))
                    WHERE id=NEW.id;
                INSERT OR IGNORE INTO source_outbox(session_id,source_uid,revision,state,created_at)
                    SELECT session_id,source_uid,revision,'revised',unixepoch('now') FROM source_ledger
                    WHERE session_id=OLD.session_id
                    AND source_uid=COALESCE(OLD.source_uid,'local:'||OLD.session_id||':'||OLD.id);
            END;
        """)
        # Assign references only to raw rows that actually exist. This does not
        # invent source snapshots for legacy summaries.
        await db.execute("""
            INSERT OR IGNORE INTO source_ledger(session_id,source_uid,message_id,revision,state,sender_id,role,updated_at)
            SELECT session_id,COALESCE(source_uid,'local:'||session_id||':'||id),id,
                COALESCE(json_extract(metadata,'$.revision'),0),'active',sender_id,role,?
            FROM messages
        """, (time.time(),))
        await db.commit()
        self._source_dispatch_lock = asyncio.Lock()

    async def sources_current(self, references):
        """Missing, revoked, changed or impersonated sources fail closed."""
        if self.connection is None:
            raise RuntimeError("source authority is unavailable")
        for item in references:
            ref = source_reference(item)
            async with self.connection.execute(
                "SELECT l.revision,l.state,l.sender_id,l.role,l.content_hash,m.content FROM source_ledger l"
                " LEFT JOIN messages m ON m.id=l.message_id WHERE l.session_id=? AND l.source_uid=?",
                (ref["session_id"], ref["source_uid"]),
            ) as cursor:
                current = await cursor.fetchone()
            if (current is None or current["state"] != "active"
                or current["revision"] != ref["revision"]
                or current["sender_id"] != ref["sender_id"] or current["role"] != ref["role"]):
                return False
            digest = (source_reference({"content": current["content"]})["content_hash"]
                      if current["content"] is not None else current["content_hash"])
            if not ref["content_hash"] or ref["content_hash"] != digest:
                return False
        return True

    async def dispatch_source_events(self, consumer, limit=200):
        """At-least-once delivery; errors retain a bounded, inspectable retry."""
        async with self._source_dispatch_lock:
            async with self.connection.execute(
                "SELECT * FROM source_outbox WHERE status IN ('pending','failed') AND next_attempt_at<=? "
                "ORDER BY id LIMIT ?", (time.time(), limit)
            ) as cursor:
                events = await cursor.fetchall()
            for event in events:
                try:
                    await consumer(dict(event))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    async with self._write_lock:
                        await self.connection.execute(
                            "UPDATE source_outbox SET attempts=attempts+1,error=?,status='pending',"
                            "next_attempt_at=? WHERE id=?",
                            (type(exc).__name__, time.time() + min(300, 2 ** min(event["attempts"] + 1, 8)), event["id"]),
                        )
                        await self.connection.commit()
                    continue
                async with self._write_lock:
                    await self.connection.execute(
                        "UPDATE source_outbox SET status='completed',error=NULL WHERE id=?", (event["id"],)
                    )
                    await self.connection.commit()
            return len(events)

    async def cleanup_completed_auxiliary(self, retention_days=30, limit=200):
        """Bound terminal bookkeeping; keep tombstones, retry keys and blocked jobs."""
        cutoff = time.time() - max(1, retention_days) * 86400
        async with self._write_lock:
            outbox = await self.connection.execute(
                "DELETE FROM source_outbox WHERE id IN (SELECT id FROM source_outbox "
                "WHERE status='completed' AND created_at<? ORDER BY id LIMIT ?)", (cutoff, limit),
            )
            jobs = await self.connection.execute(
                "DELETE FROM summary_jobs WHERE id IN (SELECT id FROM summary_jobs "
                "WHERE status IN ('completed','cancelled') AND updated_at<? "
                "AND id NOT IN (SELECT blocked_job_id FROM reflection_dirty WHERE blocked_job_id IS NOT NULL) "
                "ORDER BY id LIMIT ?)", (cutoff, limit),
            )
            await self.connection.commit()
            return {"outbox": outbox.rowcount, "jobs": jobs.rowcount}

    async def revoke_source(self, session_id, source_uid):
        """Revoke and repair positional metadata in the same raw-store transaction."""
        async with self._write_lock:
            try:
                cursor = await self.connection.execute(
                    "SELECT state FROM source_ledger WHERE session_id=? AND source_uid=?",
                    (session_id, source_uid),
                )
                previous = await cursor.fetchone()
                if previous and previous["state"] == "revoked":
                    return False
                cursor = await self.connection.execute(
                    "SELECT id,COALESCE(source_uid,'local:'||session_id||':'||id) AS uid "
                    "FROM messages WHERE session_id=? ORDER BY timestamp,id", (session_id,),
                )
                ordered = await cursor.fetchall()
                positions = [i for i, row in enumerate(ordered) if row["uid"] == source_uid]
                cursor = await self.connection.execute(
                    "SELECT metadata FROM sessions WHERE session_id=?", (session_id,),
                )
                session = await cursor.fetchone()
                metadata = json.loads(session["metadata"] or "{}") if session else {}
                def shifted(value):
                    value = max(0, int(value or 0))
                    return max(0, value - sum(index < value for index in positions))
                metadata["last_summarized_index"] = shifted(metadata.get("last_summarized_index", 0))
                pending = metadata.get("pending_summary")
                if isinstance(pending, dict):
                    pending = dict(pending)
                    pending["start_index"] = shifted(pending.get("start_index", 0))
                    pending["end_index"] = shifted(pending.get("end_index", 0))
                    metadata["pending_summary"] = pending
                if positions:
                    metadata["source_revision"] = int(metadata.get("source_revision", 0)) + 1
                await self.connection.execute(
                    "INSERT INTO source_ledger(session_id,source_uid,revision,state,updated_at)"
                    " VALUES(?,?,1,'revoked',?) ON CONFLICT(session_id,source_uid) DO UPDATE SET "
                    "state='revoked',revision=source_ledger.revision+1,updated_at=excluded.updated_at",
                    (session_id, source_uid, time.time()),
                )
                await self.connection.execute(
                    "INSERT OR IGNORE INTO source_outbox(session_id,source_uid,revision,state,created_at)"
                    " SELECT session_id,source_uid,revision,state,? FROM source_ledger"
                    " WHERE session_id=? AND source_uid=?", (time.time(), session_id, source_uid),
                )
                await self.connection.execute(
                    "DELETE FROM messages WHERE session_id=? "
                    "AND COALESCE(source_uid,'local:'||session_id||':'||id)=?", (session_id, source_uid)
                )
                if positions:
                    await self.connection.execute(
                        "UPDATE sessions SET message_count=(SELECT COUNT(*) FROM messages WHERE session_id=?),"
                        "metadata=? WHERE session_id=?", (session_id, json.dumps(metadata), session_id),
                    )
                cursor = await self.connection.execute(
                    "SELECT id,payload FROM summary_jobs WHERE session_id=? AND status IN ('pending','running')",
                    (session_id,))
                cancelled = False
                for job in await cursor.fetchall():
                    try:
                        payload = json.loads(job["payload"])
                        affected = any(ref["source_uid"] == source_uid for ref in payload["sources"])
                        # Deleting before a window moves its positions as well.
                        affected |= bool(positions and int(payload["end"]) > min(positions))
                    except (ValueError, KeyError, TypeError):
                        affected = True  # Unreadable old jobs cannot be trusted.
                    if affected:
                        cancelled = True
                        await self.connection.execute(
                            "UPDATE summary_jobs SET status='cancelled',error='source_window_changed',updated_at=? WHERE id=?",
                            (time.time(), job["id"]))
                if positions or cancelled:
                    await self.connection.execute(
                        "UPDATE reflection_dirty SET generation=generation+1,blocked_job_id=NULL,next_attempt_at=0,"
                        "updated_at=? WHERE session_id=?", (time.time(), session_id),
                    )
                await self.connection.commit()
                return True
            except BaseException:
                await self.connection.rollback()
                raise
