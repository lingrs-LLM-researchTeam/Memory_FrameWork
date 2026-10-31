"""Shared visibility gate. Search indexes and cached metadata are not authority."""

from dataclasses import replace
import math
import time

from .utils.json_utils import safe_json_dict


def is_visible(metadata, session_id=None, persona_id=None):
    metadata = safe_json_dict(metadata)
    if metadata.get("status", "active") != "active":
        return False
    if session_id is not None and metadata.get("session_id") != session_id:
        return False
    if persona_id is not None and metadata.get("persona_id") != persona_id:
        return False
    now = time.time()
    for field, is_end in (("expires_at", True), ("valid_to", True), ("valid_from", False)):
        value = metadata.get(field)
        if value is None:
            continue
        try:
            value = float(value)
        except (ValueError, TypeError):
            return False
        if not math.isfinite(value) or (value <= now if is_end else value > now):
            return False
    return True


async def revalidate_results(results, storage, session_id=None, persona_id=None):
    """Reload primary documents, dropping missing/invisible rows and stale text.

    Database errors propagate: an unavailable authority must not unlock a cache.
    """
    if not results:
        return []
    ids = list(dict.fromkeys(int(result.doc_id) for result in results))
    current = {}
    for start in range(0, len(ids), 400):
        batch = ids[start:start + 400]
        documents = await storage.get_documents(
            metadata_filters={}, ids=batch, limit=len(batch)
        )
        for document in documents:
            metadata = safe_json_dict(document.get("metadata"))
            if is_visible(metadata, session_id, persona_id):
                current[int(document["id"])] = (document, metadata)
    validated = []
    for result in results:
        row = current.get(int(result.doc_id))
        if row is not None:
            document, metadata = row
            validated.append(replace(
                result, content=str(document.get("text") or ""), metadata=metadata
            ))
    return validated
