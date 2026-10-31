"""Durable source identities and retry keys, independent of LLM-generated text."""

import asyncio
from functools import wraps
import hashlib
import inspect
import json

from .memory_source import serialize_source_messages


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def source_metadata(event, role, content):
    """Trust transport identifiers only; never merge equal-looking messages."""
    origin = getattr(event, "unified_msg_origin", "")
    message = getattr(event, "message_obj", None)
    platform_id = getattr(message, "message_id", None)
    if not isinstance(platform_id, (str, int)) or isinstance(platform_id, bool):
        platform_id = None
    result = {
        "source_scope": str(origin),
        "platform_instance": str(origin).split(":", 1)[0],
        "content_sha256": fingerprint(str(content)),
    }
    # An assistant response shares its triggering event but has no platform ID
    # until delivery. It must not steal the user's identity or deduplication key.
    if role == "user" and platform_id is not None and str(platform_id).strip():
        result["platform_message_id"] = str(platform_id)
        result["source_uid"] = fingerprint([str(origin), role, str(platform_id)])
    parts = getattr(message, "message", None)
    for part in parts if isinstance(parts, (list, tuple)) else []:
        if type(part).__name__ == "Reply":
            reply_id = getattr(part, "id", None)
            if isinstance(reply_id, (str, int)) and not isinstance(reply_id, bool):
                result["reply_to_platform_message_id"] = str(reply_id)
                break
    return result


def idempotent_source_write(method):
    """Serialize source-window retries and reuse a previously published record.

    The unique database index is a second guard. A partial, unpublished write is
    deliberately blocked pending repair rather than duplicated or made visible.
    """
    signature = inspect.signature(method)

    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        from .utils.json_utils import safe_json_dict
        bound = signature.bind(self, *args, **kwargs)
        bound.apply_defaults()
        source = serialize_source_messages(bound.arguments.get("source_messages") or [])
        if not source:
            return await method(self, *args, **kwargs)
        scopes = {row["session_id"] for row in source}
        if scopes == {""}:
            # Manual imports of old evidence may lack origin. Never infer its
            # origin, identity or retry key from the content.
            metadata = dict(bound.arguments.get("metadata") or {})
            metadata["source_provenance"] = "unverified_legacy"
            bound.arguments["metadata"] = metadata
            return await method(*bound.args, **bound.kwargs)
        if len(scopes) != 1 or not next(iter(scopes)):
            raise ValueError("source messages must belong to one known conversation")
        scope = bound.arguments["session_id"]
        source_scope = next(iter(scopes))
        if scope != source_scope:
            if not isinstance(scope, str) or not scope.startswith("livingmemory:"):
                raise ValueError("source conversation does not match memory scope")
            allowed = self.config.get("shared_source_sessions", [])
            if not isinstance(allowed, list) or source_scope not in allowed:
                raise ValueError("source conversation is not authorized for shared scope")
            if scope == "livingmemory:global":
                pass
            elif isinstance(scope, str) and scope.startswith("livingmemory:user:"):
                identity = scope.removeprefix("livingmemory:user:").split(":", 1)
                if (len(identity) != 2 or source_scope.split(":", 1)[0] != identity[0]
                    or any(row["role"] != "user" or row["sender_id"] != identity[1] for row in source)):
                    raise ValueError("source identity does not match user scope")
            else:
                raise ValueError("source conversation does not match memory scope")
        for row in source:
            context = row["metadata"].get("reflection_context")
            if context and (
                context.get("memory_scope") != scope
                or context.get("persona_id") != bound.arguments["persona_id"]
            ):
                raise ValueError("source capture persona/scope does not match memory")
        if not await self._source_rows_current(source):
            raise ValueError("source was revoked, revised or cannot be verified")
        identity_rows = [
            [row["id"], row["role"], row["sender_id"], row["timestamp"],
             row["metadata"].get("source_uid"), row["metadata"].get("revision", 0), row["content"]]
            for row in source
        ]
        digest = fingerprint(["source-v1", scope, bound.arguments["persona_id"],
                              source_scope, identity_rows])
        metadata = dict(bound.arguments.get("metadata") or {})
        root_digest = digest
        replacing = bool(bound.arguments["preserve_create_time"] and metadata.get("previous_id"))
        if replacing:
            digest = fingerprint([root_digest, "revision", metadata["previous_id"],
                                  bound.arguments["content"]])
        metadata.update(
            source_digest=digest, source_session_id=source_scope,
            source_root_digest=root_digest,
            epistemic_status="unverified_summary",
            source_reporters=sorted({row["sender_id"] for row in source if row["role"] == "user"}),
        )
        bound.arguments["metadata"] = metadata
        if not hasattr(self, "_source_write_lock"):
            self._source_write_lock = asyncio.Lock()
        async with self._source_write_lock:
            if self.db_connection is None:
                raise RuntimeError("memory database is not ready")
            sql = "SELECT id, metadata FROM documents WHERE json_extract(metadata, '$.source_digest') = ?"
            params = [digest]
            if not replacing:
                sql += " OR json_extract(metadata, '$.source_root_digest') = ?"
                params.append(root_digest)
            cursor = await self.db_connection.execute(sql + " ORDER BY id DESC LIMIT 1", params)
            row = await cursor.fetchone()
            if row:
                status = safe_json_dict(row[1]).get("status", "active")
                if status in {"active", "archived"}:
                    return int(row[0])
                raise RuntimeError("source window has an unpublished write pending repair")
            return await method(*bound.args, **bound.kwargs)
    return wrapped
