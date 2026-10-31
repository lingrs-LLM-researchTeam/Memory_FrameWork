"""Source-to-memory dependencies and revocation, independent of derived indexes."""
import asyncio
import json
import time

from ..memory_source import serialize_source_messages, source_reference
from ..utils.json_utils import safe_json_dict


class MemoryEngineSourcesMixin:
    source_store = None

    async def _create_source_tables(self):
        await self.db_connection.executescript("""
            CREATE TABLE IF NOT EXISTS memory_source_links(
                memory_id INTEGER NOT NULL, session_id TEXT NOT NULL, source_uid TEXT NOT NULL,
                revision INTEGER NOT NULL, sender_id TEXT NOT NULL, role TEXT NOT NULL,
                platform_message_id TEXT, reply_to TEXT, content_hash TEXT NOT NULL,
                PRIMARY KEY(memory_id,session_id,source_uid,revision)
            );
            CREATE INDEX IF NOT EXISTS idx_memory_source_reverse
                ON memory_source_links(session_id,source_uid,revision);
            CREATE TABLE IF NOT EXISTS source_tombstones(
                session_id TEXT NOT NULL,source_uid TEXT NOT NULL,revision INTEGER NOT NULL,
                state TEXT NOT NULL,updated_at REAL NOT NULL,
                PRIMARY KEY(session_id,source_uid)
            );
            CREATE TRIGGER IF NOT EXISTS memory_source_delete AFTER DELETE ON documents BEGIN
                DELETE FROM memory_source_links WHERE memory_id=OLD.id;
            END;
        """)

    async def bind_source_store(self, store):
        self._source_authority_required = True
        self.source_store = store
        await store.dispatch_source_events(self.consume_source_event)
        async def worker():
            maintenance_at = 0
            reconcile_at = 0
            while True:
                await asyncio.sleep(2)
                try:
                    await store.dispatch_source_events(self.consume_source_event)
                    reconcile = getattr(getattr(self, "source_event_sink", None), "reconcile", None)
                    if callable(reconcile) and time.monotonic() >= reconcile_at:
                        reconcile_at = time.monotonic() + 30
                        await reconcile()
                    if time.monotonic() >= maintenance_at:
                        await store.cleanup_completed_auxiliary()
                        # A separate connection must not commit an in-flight write.
                        import aiosqlite
                        async with aiosqlite.connect(self.db_path) as maintenance:
                            await maintenance.execute("PRAGMA busy_timeout=1000")
                            await maintenance.execute(
                                "DELETE FROM memory_write_ops WHERE id IN (SELECT id FROM memory_write_ops "
                                "WHERE status='completed' AND updated_at<? ORDER BY id LIMIT 200)",
                                (time.time() - 30 * 86400,),
                            )
                            await maintenance.commit()
                        maintenance_at = time.monotonic() + 3600
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Recall still checks the raw authority and fails closed.
                    from astrbot.api import logger
                    logger.warning("来源事件队列暂不可用，保留待处理任务", exc_info=True)
        self._create_tracked_task(worker())

    async def _source_rows_current(self, rows):
        refs = [source_reference(row) for row in rows]
        if any(not ref["source_uid"] or not ref["session_id"] for ref in refs):
            return False
        if self.source_store is None and getattr(self, "_source_authority_required", False):
            raise RuntimeError("source authority is unavailable")
        if self.source_store is not None and not await self.source_store.sources_current(refs):
            return False
        for ref in refs:
            async with self.db_connection.execute(
                "SELECT revision,state FROM source_tombstones WHERE session_id=? AND source_uid=?",
                (ref["session_id"], ref["source_uid"]),
            ) as cursor:
                tombstone = await cursor.fetchone()
            if tombstone and (tombstone["state"] == "revoked" or ref["revision"] < tombstone["revision"]):
                return False
        return True

    async def memory_sources_current(self, memory_id):
        async with self.db_connection.execute(
            "SELECT * FROM memory_source_links WHERE memory_id=?", (int(memory_id),)
        ) as cursor:
            refs = [dict(row) for row in await cursor.fetchall()]
        # Legacy summaries stay explicitly unverified; no synthetic backfill.
        return not refs or await self._source_rows_current(refs)

    async def _register_memory_sources(self, memory_id, sources):
        rows = serialize_source_messages(sources)
        known = [row for row in rows if source_reference(row)["source_uid"] and row["session_id"]]
        if known and not await self._source_rows_current(known):
            raise ValueError("source was revoked or revised before publication")
        for row in known:
            ref = source_reference(row)
            await self.db_connection.execute(
                "INSERT OR IGNORE INTO memory_source_links VALUES(?,?,?,?,?,?,?,?,?)",
                (memory_id, ref["session_id"], ref["source_uid"], ref["revision"],
                 ref["sender_id"], ref["role"], ref["platform_message_id"], ref["reply_to"], ref["content_hash"]),
            )
        return rows

    async def source_references(self, memory_id):
        async with self.db_connection.execute(
            "SELECT * FROM memory_source_links WHERE memory_id=? ORDER BY rowid", (int(memory_id),)
        ) as cursor:
            return [
                {**dict(row), "citation": "msg:" + row["source_uid"]}
                for row in await cursor.fetchall()
            ]

    async def consume_source_event(self, event):
        if event["state"] == "active":
            return
        await self.invalidate_source(
            event["session_id"], event["source_uid"],
            revision=int(event["revision"]), state=event["state"],
        )
        sink = getattr(self, "source_event_sink", None)
        if sink is not None:
            await sink(event)

    async def invalidate_source(self, session_id, source_uid, *, revision=1, state="revoked"):
        if state not in {"revoked", "revised"}:
            raise ValueError("invalid source event")
        await self.db_connection.execute(
            "INSERT INTO source_tombstones VALUES(?,?,?,?,?) ON CONFLICT(session_id,source_uid)"
            " DO UPDATE SET revision=MAX(source_tombstones.revision,excluded.revision),"
            " state=CASE WHEN source_tombstones.state='revoked' THEN 'revoked' ELSE excluded.state END,"
            " updated_at=excluded.updated_at", (session_id, source_uid, revision, state, time.time()),
        )
        async with self.db_connection.execute(
            "SELECT DISTINCT d.id,d.metadata FROM documents d JOIN memory_source_links s ON s.memory_id=d.id"
            " WHERE s.session_id=? AND s.source_uid=? AND (?='revoked' OR s.revision<?)",
            (session_id, source_uid, state, revision),
        ) as cursor:
            documents = await cursor.fetchall()
        ids = []
        for doc in documents:
            metadata = safe_json_dict(doc["metadata"])
            metadata.update(status="source_revoked", source_invalidated_at=time.time())
            await self.db_connection.execute(
                "UPDATE documents SET metadata=? WHERE id=?", (json.dumps(metadata, ensure_ascii=False), doc["id"])
            )
            ids.append(int(doc["id"]))
        await self.db_connection.commit()
        self._invalidate_search_cache()
        # Visibility is already revoked. Cleanup is a repairable optimization.
        for mid in ids:
            if self.graph_memory_manager is not None:
                await self.graph_memory_manager.delete_memory(mid)
        return len(ids)

    async def _publish_sourced_memory(self, memory_id, metadata):
        if not await self.memory_sources_current(memory_id):
            raise ValueError("source was revoked or revised before publication")
        status = ("quarantined" if metadata.get("claim_schema_version") == 1 and not metadata.get("claims")
                  else "active")
        metadata = {**metadata, "status": status, "source_schema_version": 2}
        metadata["source_refs"] = [
            {"source_uid": ref["source_uid"], "revision": ref["revision"], "citation": ref["citation"]}
            for ref in await self.source_references(memory_id)
        ]
        cursor = await self.db_connection.execute(
            "UPDATE documents SET metadata=? WHERE id=? AND json_extract(metadata,'$.status')='pending_source'",
            (json.dumps(metadata, ensure_ascii=False), memory_id),
        )
        await self.db_connection.commit()
        if cursor.rowcount != 1:
            raise ValueError("memory state changed before source publication")
        return metadata
