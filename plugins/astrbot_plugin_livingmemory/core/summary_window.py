"""Keep positional progress separate from the user evidence used by a summary."""
from dataclasses import dataclass
from .memory_source import source_reference


@dataclass(frozen=True)
class SummaryWindow:
    """A job's exact positional source window plus the manager's reset epoch."""
    session_id: str
    start: int
    end: int
    sources: tuple
    reset_epoch: int

    async def is_current(self, store):
        if self.start < 0 or self.end <= self.start or self.end - self.start != len(self.sources):
            return False
        cursor = await store.connection.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY timestamp,id LIMIT ? OFFSET ?",
            (self.session_id, self.end - self.start, self.start))
        actual = tuple(source_reference(dict(row)) for row in await cursor.fetchall())
        return actual == self.sources and await store.sources_current(self.sources)


def capture_window(messages, persona, scope):
    """Take one captured persona/scope segment, retaining its raw row positions."""
    if not messages:
        return [], persona, scope
    context = messages[0].metadata.get("reflection_context") or {
        "persona_id": persona, "memory_scope": scope,
    }
    end = len(messages)
    for index, message in enumerate(messages):
        if message.metadata.get("reflection_context_status") == "unresolved":
            end = index
            break
        other = message.metadata.get("reflection_context")
        if other and other != context:
            end = index
            break
    return messages[:end], context["persona_id"], context["memory_scope"]


def summary_evidence(messages, scope):
    if not isinstance(scope, str) or not scope.startswith("livingmemory:user:"):
        return messages
    instance, sender = scope.removeprefix("livingmemory:user:").split(":", 1)
    users = [m for m in messages if m.role == "user"]
    if any(m.sender_id != sender or m.session_id.split(":", 1)[0] != instance for m in users):
        raise ValueError("mixed user identities must be split before summarization")
    # An assistant turn is part of cursor progress, never a statement by the user.
    return users
