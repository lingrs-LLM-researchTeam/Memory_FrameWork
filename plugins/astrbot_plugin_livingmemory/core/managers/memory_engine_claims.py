"""Per-claim evidence and conservative conflict sets; no automatic truth overwrite."""
import json
import time

from ..claim_evidence import validate_claims, claim_text
from ..utils.json_utils import safe_json_dict
from ..recall_policy import is_visible


class MemoryEngineClaimsMixin:
    async def _create_claim_tables(self):
        await self.db_connection.executescript("""
            CREATE TABLE IF NOT EXISTS memory_claims(
                memory_id INTEGER NOT NULL,claim_id TEXT NOT NULL,
                session_id TEXT,persona_id TEXT,subject_id TEXT,predicate TEXT,value TEXT,
                valid_from REAL,valid_to REAL,claim_json TEXT NOT NULL,
                PRIMARY KEY(memory_id,claim_id)
            );
            CREATE INDEX IF NOT EXISTS idx_claim_subject
                ON memory_claims(session_id,persona_id,subject_id,predicate);
            CREATE TABLE IF NOT EXISTS claim_conflicts(
                left_memory_id INTEGER,left_claim_id TEXT,right_memory_id INTEGER,right_claim_id TEXT,
                PRIMARY KEY(left_memory_id,left_claim_id,right_memory_id,right_claim_id)
            );
            CREATE TRIGGER IF NOT EXISTS memory_claim_delete AFTER DELETE ON documents BEGIN
                DELETE FROM memory_claims WHERE memory_id=OLD.id;
                DELETE FROM claim_conflicts WHERE left_memory_id=OLD.id OR right_memory_id=OLD.id;
            END;
        """)

    async def _register_claims(self, memory_id, metadata, sources):
        if metadata.get("claim_schema_version") != 1:
            return metadata
        claims = validate_claims(metadata.get("claims"), sources)
        metadata = {**metadata, "claims": claims}
        for claim in claims:
            await self.db_connection.execute(
                "INSERT OR IGNORE INTO memory_claims VALUES(?,?,?,?,?,?,?,?,?,?)",
                (memory_id, claim["claim_id"], metadata.get("session_id"), metadata.get("persona_id"),
                 claim["subject_id"], claim["predicate"], claim["value"], claim["valid_from"],
                 claim["valid_to"], json.dumps(claim, ensure_ascii=False)),
            )
            if not claim["subject_id"] or not claim["value"]:
                continue
            # Preserve differing reports with overlapping applicability. Neither
            # arrival order nor a third party can supersede another speaker.
            async with self.db_connection.execute(
                "SELECT c.memory_id,c.claim_id FROM memory_claims c JOIN documents d ON d.id=c.memory_id"
                " WHERE c.session_id IS ? AND c.persona_id IS ? AND c.subject_id=? AND c.predicate=?"
                " AND c.value!=? AND json_extract(d.metadata,'$.status') IN ('active','pending_source')"
                " AND COALESCE(c.valid_to,1e30)>COALESCE(?,0)"
                " AND COALESCE(?,1e30)>COALESCE(c.valid_from,0)",
                (metadata.get("session_id"), metadata.get("persona_id"), claim["subject_id"],
                 claim["predicate"], claim["value"], claim["valid_from"], claim["valid_to"]),
            ) as cursor:
                conflicts = await cursor.fetchall()
            for other in conflicts:
                await self.db_connection.execute(
                    "INSERT OR IGNORE INTO claim_conflicts VALUES(?,?,?,?)",
                    (memory_id, claim["claim_id"], other["memory_id"], other["claim_id"]),
                )
        await self.db_connection.commit()
        return metadata

    async def live_claims(self, memory_id, atom_rows=None):
        now = time.time()
        async with self.db_connection.execute(
            "SELECT claim_json FROM memory_claims WHERE memory_id=?"
            " AND (valid_from IS NULL OR valid_from<=?) AND (valid_to IS NULL OR valid_to>?)",
            (memory_id, now, now),
        ) as cursor:
            claims = [json.loads(row[0]) for row in await cursor.fetchall()]
        if atom_rows is not None:
            active_texts = {str(row[2]) for row in atom_rows}
            active_claim_ids = {
                safe_json_dict(row[5]).get("claim_id") for row in atom_rows if len(row) > 5
            }
            claims = [claim for claim in claims
                      if claim["claim_id"] in active_claim_ids or claim_text(claim) in active_texts]
        for claim in claims:
            async with self.db_connection.execute(
                "SELECT left_memory_id,left_claim_id,right_memory_id,right_claim_id FROM claim_conflicts"
                " WHERE (left_memory_id=? AND left_claim_id=?) OR (right_memory_id=? AND right_claim_id=?)",
                (memory_id, claim["claim_id"], memory_id, claim["claim_id"]),
            ) as cursor:
                conflicts = await cursor.fetchall()
            claim["conflicts_with"] = []
            for pair in conflicts:
                other = pair[2] if pair[0] == memory_id and pair[1] == claim["claim_id"] else pair[0]
                doc = await self.get_memory_for_recall(other)
                if doc and is_visible(doc["metadata"]) and await self.memory_sources_current(other):
                    claim["conflicts_with"].append(other)
        return claims

    @staticmethod
    def render_claims(claims):
        return [
            claim_text(claim) + (f" [存在冲突，关联记忆 {claim['conflicts_with']}，不得自行裁定]" if claim.get("conflicts_with") else "")
            for claim in claims
        ]
