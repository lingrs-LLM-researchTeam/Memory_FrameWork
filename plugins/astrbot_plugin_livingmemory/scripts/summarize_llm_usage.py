"""Summarize LivingMemory usage logs offline; no AstrBot startup or network."""
import argparse
import importlib.util
import json
from pathlib import Path


def summarize(paths):
    module_path = Path(__file__).resolve().parents[1] / "core/processors/llm_usage.py"
    spec = importlib.util.spec_from_file_location("livingmemory_usage_reader", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    recorder = module.UsageRecorder()
    seen = set()
    malformed = duplicates = 0
    for path in paths:
        with Path(path).open(encoding="utf-8-sig", errors="replace") as handle:
            for line in handle:
                _, marker, payload = line.partition("[LivingMemoryUsage] ")
                if not marker:
                    continue
                try:
                    event, _ = json.JSONDecoder().raw_decode(payload.strip())
                    if event.get("schema_version") != 1 or not isinstance(event.get("request_id"), str):
                        raise ValueError("Unknown schema or missing request_id")
                    if event["request_id"] in seen:
                        duplicates += 1
                        continue
                    recorder.add_event(event)
                    seen.add(event["request_id"])
                except (ValueError, TypeError, KeyError, AttributeError):
                    malformed += 1
    return {"schema_version": 1, "observed_attempts": len(seen), "duplicate_events": duplicates,
            "malformed_events": malformed, "groups": recorder.snapshot()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    text = json.dumps(summarize(args.logs), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
