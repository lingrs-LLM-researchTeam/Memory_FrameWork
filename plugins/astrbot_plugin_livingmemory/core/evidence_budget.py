"""A request-local byte ceiling shared by automatic injection and recall tools.

UTF-8 bytes deliberately overestimate typical byte-tokenizer text usage. The
host's context limit must still cover system text, history, tools and output.
"""

import asyncio
from functools import wraps
import json

from .memory_scope import _config_get

_ATTR = "_livingmemory_evidence_bytes"


def claimed_evidence(event):
    value = getattr(event, "_livingmemory_claimed_evidence", None)
    if not isinstance(value, set):
        value = set()
        setattr(event, "_livingmemory_claimed_evidence", value)
    return value


def budget_for(event, config):
    try:
        cap = int(_config_get(config, "agent_tools.max_total_evidence_bytes", 0))
    except (ValueError, TypeError):
        cap = 0
    if cap <= 0:
        return None
    state = getattr(event, _ATTR, None)
    if not isinstance(state, dict):
        state = {"cap": cap, "used": 0, "lock": asyncio.Lock()}
        setattr(event, _ATTR, state)
    return state


def automatic_allowance(event, config):
    state = budget_for(event, config)
    if state is None:
        return 7000
    # Reserve space for source lookup if automatic retrieval is insufficient.
    return min(3500, max(0, state["cap"] - state["used"]))


def charge_automatic(event, config, text):
    state = budget_for(event, config)
    if state is not None:
        state["used"] += len(text.encode("utf-8"))


def bounded_evidence_call(method):
    @wraps(method)
    async def wrapped(self, context, *args, **kwargs):
        event = getattr(getattr(context, "context", None), "event", None)
        state = budget_for(event, self.config_manager) if event is not None else None
        if state is None:
            return await method(self, context, *args, **kwargs)
        # Parallel tool calls must not spend the same remaining allowance twice.
        async with state["lock"]:
            remaining = max(0, state["cap"] - state["used"])
            exhausted = '{"status":"evidence_budget_exhausted","results":[]}'
            if remaining < 500:
                text = exhausted if len(exhausted) <= remaining else ""
            else:
                from .tools.agentic_recall_budget import get_or_create_budget
                budget = get_or_create_budget(event, self.config_manager)
                old_cap = budget.max_result_chars
                budget.max_result_chars = min(old_cap, max(500, remaining // 4))
                try:
                    text = await method(self, context, *args, **kwargs)
                finally:
                    budget.max_result_chars = old_cap
                if isinstance(text, str) and len(text.encode("utf-8")) > remaining:
                    # Legacy tool outputs have a list of whole entries. Preserve
                    # complete identities/provenance, never slice arbitrary text.
                    try:
                        payload = json.loads(text)
                        rows = payload.get("results")
                        while isinstance(rows, list) and rows and len(text.encode("utf-8")) > remaining:
                            rows.pop()
                            payload["count"] = len(rows)
                            payload["truncated"] = True
                            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    except (ValueError, AttributeError):
                        pass
                    if len(text.encode("utf-8")) > remaining:
                        text = exhausted
            if isinstance(text, str):
                state["used"] += len(text.encode("utf-8"))
            return text
    return wrapped
