"""Transport lifecycle events. Message text is never interpreted as an event."""
from collections.abc import Mapping

from astrbot.api.event.filter import CustomFilter
from .source_identity import fingerprint


def recalled_source(event):
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if not isinstance(raw, Mapping):
        return None
    if raw.get("post_type") != "notice" or raw.get("notice_type") not in {"group_recall", "friend_recall"}:
        return None
    message_id = raw.get("message_id")
    origin = getattr(event, "unified_msg_origin", "")
    if (not isinstance(origin, str) or not origin
        or not isinstance(message_id, (int, str)) or isinstance(message_id, bool)
        or not str(message_id).strip()):
        return None
    return origin, fingerprint([origin, "user", str(message_id)])


class SourceLifecycleFilter(CustomFilter):
    def filter(self, event, cfg):
        return recalled_source(event) is not None


async def apply_source_recall(event, engine, store, manager=None):
    source = recalled_source(event)
    if source is None:
        return False
    await (manager or store).revoke_source(*source)
    await store.dispatch_source_events(engine.consume_source_event)
    return True
