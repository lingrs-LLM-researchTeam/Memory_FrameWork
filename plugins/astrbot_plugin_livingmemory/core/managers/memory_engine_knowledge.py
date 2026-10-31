"""Reviewed knowledge as live references, never detached copies of chat evidence."""
import time
import re

from ..recall_policy import is_visible
from ..source_identity import fingerprint


class MemoryEngineKnowledgeMixin:
    async def _create_knowledge_table(self):
        await self.db_connection.execute("""
            CREATE TABLE IF NOT EXISTS reviewed_knowledge(
                id INTEGER PRIMARY KEY AUTOINCREMENT,memory_id INTEGER NOT NULL,
                source_scope TEXT NOT NULL,target_scope TEXT NOT NULL,persona_id TEXT NOT NULL,
                evidence_digest TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
                proposed_by TEXT NOT NULL,reviewed_by TEXT,updated_at REAL NOT NULL
            )
        """)

    @staticmethod
    def _knowledge_digest(metadata):
        return fingerprint([metadata.get("source_digest"), metadata.get("claims")])

    async def propose_knowledge(self, memory_id, source_scope, target_scope, persona_id, actor_id):
        doc = await self.get_memory_for_recall(memory_id)
        if (not doc or not source_scope or not target_scope or not persona_id or not actor_id
            or not is_visible(doc["metadata"], source_scope, persona_id)
            or doc["metadata"].get("claim_schema_version") != 1
            or not await self.source_references(memory_id)
            or not await self.memory_sources_current(memory_id)
            or not await self.resolve_live_facts(memory_id, doc["text"], doc["metadata"])):
            raise ValueError("knowledge proposal requires current, scoped, source-backed claims")
        cursor = await self.db_connection.execute(
            "INSERT INTO reviewed_knowledge(memory_id,source_scope,target_scope,persona_id,evidence_digest,"
            "proposed_by,updated_at) VALUES(?,?,?,?,?,?,?)",
            (memory_id, source_scope, target_scope, persona_id,
             self._knowledge_digest(doc["metadata"]), actor_id, time.time()),
        )
        await self.db_connection.commit()
        return int(cursor.lastrowid)

    async def review_knowledge(self, publication_id, source_scope, persona_id, actor_id, approve):
        async with self.db_connection.execute(
            "SELECT * FROM reviewed_knowledge WHERE id=? AND source_scope=? AND persona_id=?",
            (publication_id, source_scope, persona_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or not actor_id:
            raise ValueError("knowledge proposal not found in this scope")
        doc = await self.get_memory_for_recall(row["memory_id"])
        if approve and (
            not doc or not is_visible(doc["metadata"], source_scope, persona_id)
            or self._knowledge_digest(doc["metadata"]) != row["evidence_digest"]
            or not await self.memory_sources_current(row["memory_id"])
        ):
            raise ValueError("knowledge source changed; propose it again")
        await self.db_connection.execute(
            "UPDATE reviewed_knowledge SET status=?,reviewed_by=?,updated_at=? WHERE id=?",
            ("approved" if approve else "revoked", actor_id, time.time(), publication_id),
        )
        await self.db_connection.commit()

    async def search_reviewed_knowledge(self, query, session_id, persona_id):
        if not session_id or not persona_id or not query.strip():
            return []
        async with self.db_connection.execute(
            "SELECT * FROM reviewed_knowledge WHERE status='approved' AND target_scope=? AND persona_id=?"
            " ORDER BY updated_at DESC LIMIT 100", (session_id, persona_id),
        ) as cursor:
            publications = await cursor.fetchall()
        results = []
        terms = set(re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]", query.casefold()))
        for row in publications:
            # Load only explicitly published records. No retrieval or reranking
            # call may inspect other records in the source conversation.
            mid = row["memory_id"]
            doc = await self.get_memory_for_recall(mid)
            if (not doc or not is_visible(doc["metadata"], row["source_scope"], persona_id)
                or self._knowledge_digest(doc["metadata"]) != row["evidence_digest"]
                or not await self.memory_sources_current(mid)):
                continue
            facts = await self.resolve_live_facts(mid, doc["text"], doc["metadata"])
            text = "\n".join(facts or []).casefold()
            score = sum(term in text for term in terms)
            if facts and score:
                results.append({
                    "publication_id": row["id"], "memory_id": mid,
                    "facts": facts, "reviewed_by": row["reviewed_by"],
                    "source_refs": await self.source_references(mid),
                    "evidence_status": "reviewed_publication_not_independent_verification",
                    "match_score": score,
                })
        return sorted(results, key=lambda hit: hit["match_score"], reverse=True)[:10]
