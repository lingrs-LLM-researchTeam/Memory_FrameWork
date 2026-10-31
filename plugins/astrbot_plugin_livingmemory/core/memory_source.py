"""Structured source-conversation helpers for durable important memories."""

from __future__ import annotations

from typing import Any

from .models.conversation_models import Message
import hashlib
import json


def source_reference(item):
    """Stable, transport-scoped reference; content never defines message identity."""
    metadata = item.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    scope = str(item.get("session_id") or "")
    uid = item.get("source_uid") or metadata.get("source_uid")
    if not uid and item.get("id"):
        uid = f"local:{scope}:{item['id']}"
    return {
        "source_uid": str(uid or ""), "session_id": scope,
        "revision": int(item.get("revision", metadata.get("revision", 0)) or 0),
        "sender_id": str(item.get("sender_id") or "unknown"),
        "role": str(item.get("role") or "user"),
        "platform_message_id": item.get("platform_message_id") or metadata.get("platform_message_id"),
        "reply_to": item.get("reply_to") or metadata.get("reply_to_platform_message_id"),
        "content_hash": (
            hashlib.sha256(json.dumps(Message.content_to_text(item["content"]), ensure_ascii=False,
                                      sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if "content" in item else str(item.get("content_hash") or "")
        ),
    }


def serialize_source_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """Store only fields required for review and deterministic re-summarization."""
    serialized: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, dict):
            data = message
        elif hasattr(message, "to_dict"):
            data = message.to_dict()
        else:
            data = {
                key: getattr(message, key, None)
                for key in (
                    "id",
                    "session_id",
                    "role",
                    "content",
                    "sender_id",
                    "sender_name",
                    "group_id",
                    "platform",
                    "timestamp",
                )
            }
        content = Message.content_to_text(data.get("content"))
        if not content:
            continue
        raw_metadata = data.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        try:
            message_id = int(data.get("id") or 0)
        except (TypeError, ValueError):
            message_id = 0
        try:
            timestamp = float(data.get("timestamp") or 0.0)
        except (TypeError, ValueError):
            timestamp = 0.0
        serialized.append(
            {
                "id": message_id,
                "session_id": str(data.get("session_id") or ""),
                "role": str(data.get("role") or "user"),
                "content": content,
                "sender_id": str(data.get("sender_id") or "unknown"),
                "sender_name": data.get("sender_name"),
                "group_id": data.get("group_id"),
                "platform": data.get("platform"),
                "timestamp": timestamp,
                "metadata": {
                    "is_bot_message": bool(metadata.get("is_bot_message", False)),
                    **{key: metadata[key] for key in (
                        "source_uid", "source_scope", "platform_instance",
                        "platform_message_id", "reply_to_platform_message_id",
                        "content_sha256", "llm_checkpoint_id", "revision", "reflection_context",
                    ) if key in metadata}
                },
            }
        )
        ref = source_reference(serialized[-1])
        serialized[-1]["metadata"].update(source_uid=ref["source_uid"], revision=ref["revision"])
    return serialized


def restore_source_messages(source: list[dict[str, Any]]) -> list[Message]:
    """Restore source rows to the model expected by MemoryProcessor."""
    return [Message.from_dict(item) for item in source]


__all__ = ["restore_source_messages", "serialize_source_messages"]
