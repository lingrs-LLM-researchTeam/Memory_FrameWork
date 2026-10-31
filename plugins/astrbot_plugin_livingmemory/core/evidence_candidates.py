"""Request-local query results. An empty result belongs to one exact query."""
import asyncio


async def find_candidate_ids(event, engine, *, query, k, session_id, persona_id):
    getter, setter = getattr(event, "get_extra", None), getattr(event, "set_extra", None)
    state = getter("livingmemory_candidate_queries", None) if callable(getter) else None
    if not isinstance(state, dict) or "lock" not in state:
        state = {"lock": asyncio.Lock(), "results": {}}
        if callable(setter):
            setter("livingmemory_candidate_queries", state)
    key = (id(engine), session_id, persona_id, query, k)
    async with state["lock"]:
        if key not in state["results"]:
            memories = await engine.search_memories(
                query=query, k=k, session_id=session_id, persona_id=persona_id)
            # Never cache evidence text: each consumer re-reads live records.
            state["results"][key] = tuple(m.doc_id for m in memories if type(m.doc_id) is int)
        return [{"memory_id": mid} for mid in state["results"][key]]
