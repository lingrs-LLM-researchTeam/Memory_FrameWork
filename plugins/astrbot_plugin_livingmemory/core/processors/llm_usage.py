"""Best-effort usage observations; never a billing ledger or a request cache."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from uuid import uuid4


def field(value, name):
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def normalize_usage(response):
    """Prefer raw counters so a missing cache field is not silently turned into 0."""
    raw = field(field(response, "raw_completion"), "usage")
    usage = raw if raw is not None else field(response, "usage")
    result = dict(input_tokens=None, cached_input_tokens=None, uncached_input_tokens=None,
                  output_tokens=None, total_tokens=None, reasoning_tokens=None,
                  usage_source="unavailable", usage_status="unavailable")
    if usage is None:
        return result

    if raw is None and field(usage, "input_other") is not None:
        other, cached, output = (count(field(usage, k)) for k in ("input_other", "input_cached", "output"))
        # AstrBot also constructs TokenUsage() when upstream usage is absent.
        # Without a raw response, an all-zero default is indistinguishable.
        if None in (other, cached, output) or other + cached + output == 0:
            return result
        result.update(input_tokens=other + cached, cached_input_tokens=cached,
                      uncached_input_tokens=other, output_tokens=output,
                      total_tokens=other + cached + output,
                      usage_source="astrbot_normalized", usage_status="reported")
        return result

    prompt = count(field(usage, "prompt_tokens"))
    if prompt is None:
        prompt = count(field(usage, "input_tokens"))
    output = count(field(usage, "completion_tokens"))
    if output is None:
        output = count(field(usage, "output_tokens"))
    cached = count(field(field(usage, "prompt_tokens_details"), "cached_tokens"))
    if cached is None:
        cached = count(field(field(usage, "input_tokens_details"), "cached_tokens"))
    standard_cache = cached is not None
    if cached is None:
        cached = count(field(usage, "prompt_cache_hit_tokens"))
    miss = count(field(usage, "prompt_cache_miss_tokens"))
    if prompt is None and cached is not None and miss is not None and not standard_cache:
        prompt = cached + miss
    if cached is None and prompt is not None and miss is not None and miss <= prompt:
        cached = prompt - miss
    inconsistent = (prompt is not None and cached is not None and cached > prompt)
    if not standard_cache and prompt is not None and cached is not None and miss is not None:
        inconsistent |= cached + miss != prompt
    if inconsistent:
        cached = None
    uncached = prompt - cached if prompt is not None and cached is not None else None
    total = count(field(usage, "total_tokens"))
    if total is None and prompt is not None and output is not None:
        total = prompt + output
    reasoning = count(field(field(usage, "completion_tokens_details"), "reasoning_tokens"))
    if reasoning is None:
        reasoning = count(field(field(usage, "output_tokens_details"), "reasoning_tokens"))
    if all(v is None for v in (prompt, output, cached, total)):
        return result
    result.update(input_tokens=prompt, cached_input_tokens=cached,
                  uncached_input_tokens=uncached, output_tokens=output, total_tokens=total,
                  reasoning_tokens=reasoning,
                  usage_source="raw_completion" if raw is not None else "response_usage",
                  usage_status="inconsistent" if inconsistent else (
                      "reported" if None not in (prompt, output, cached) else "partial"))
    return result


class UsageRecorder:
    """Bounded by task/provider/model combinations, not by request count."""

    def __init__(self):
        self._groups = {}

    def record(self, *, response, provider, operation, logical_call_id, attempt,
               transport, status, elapsed_ms, prompt, system_prompt, error=None):
        # Telemetry is deliberately fail-open: logging must never retry an LLM.
        try:
            config = field(provider, "provider_config")
            provider_id = field(config, "id")
            identity = provider_id if isinstance(provider_id, str) else type(provider).__name__
            provider_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
            model = field(field(response, "raw_completion"), "model")
            if not isinstance(model, str):
                model = field(config, "model")
            if not isinstance(model, str):
                model = "unknown"
            metrics = normalize_usage(response)
            event = dict(schema_version=1, timestamp=datetime.now(timezone.utc).isoformat(),
                         request_id=uuid4().hex, logical_call_id=logical_call_id,
                         operation=operation, provider_hash=provider_hash, model=model,
                         attempt=attempt, transport=transport, status=status,
                         elapsed_ms=round(elapsed_ms, 3),
                         system_prompt_sha256=hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
                         prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                         error_type=type(error).__name__ if error is not None else None,
                         **metrics)
            self.add_event(event)
            from astrbot.api import logger

            logger.info("[LivingMemoryUsage] " + json.dumps(event, ensure_ascii=False, separators=(",", ":")))
        except Exception:
            pass

    def add_event(self, event):
        """Aggregate a structured event (also used by the offline log reader)."""
        operation, provider_hash, model = (event[k] for k in ("operation", "provider_hash", "model"))
        status_key = {"success":"successes", "error":"errors", "cancelled":"cancellations"}[event["status"]]
        if not all(isinstance(v, str) for v in (operation, provider_hash, model)):
            raise ValueError("Invalid usage grouping fields")
        elapsed = event["elapsed_ms"]
        if not isinstance(elapsed, (int, float)) or elapsed < 0:
            raise ValueError("Invalid duration")
        key = (operation, provider_hash, model)
        row = self._groups.setdefault(key, dict(operation=operation, provider_hash=provider_hash,
            model=model, attempts=0, successes=0, errors=0, cancellations=0,
            input_observed=0, output_observed=0, cache_observed=0,
            usage_unavailable=0, input_tokens=0, output_tokens=0,
            cached_input_tokens=0, cache_observed_input_tokens=0, elapsed_ms=0.0))
        row["attempts"] += 1
        row[status_key] += 1
        row["elapsed_ms"] += elapsed
        row["usage_unavailable"] += event.get("usage_status") == "unavailable"
        for name, observed in (("input_tokens", "input_observed"), ("output_tokens", "output_observed")):
            value = count(event.get(name))
            if value is not None:
                row[name] += value
                row[observed] += 1
        prompt, cached = count(event.get("input_tokens")), count(event.get("cached_input_tokens"))
        if prompt is not None and cached is not None and cached <= prompt:
            row["cache_observed"] += 1
            row["cached_input_tokens"] += cached
            row["cache_observed_input_tokens"] += prompt

    def snapshot(self):
        rows = []
        for group in self._groups.values():
            row = dict(group)
            denominator = row["cache_observed_input_tokens"]
            row["cache_hit_ratio"] = row["cached_input_tokens"] / denominator if denominator else None
            row["cache_coverage"] = row["cache_observed"] / row["attempts"]
            row["uncached_input_tokens"] = denominator - row["cached_input_tokens"]
            rows.append(row)
        return rows
