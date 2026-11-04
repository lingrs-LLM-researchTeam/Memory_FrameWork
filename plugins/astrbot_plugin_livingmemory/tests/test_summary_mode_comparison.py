"""Offline scheduler A/B. Invoked by tools/run_regressions.py --comparison-vocab."""
import asyncio
import base64
from collections import Counter, defaultdict
from datetime import datetime
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace as NS
import unicodedata
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_livingmemory.core import summary_recovery
from astrbot_plugin_livingmemory.core.event_handler_modules.memory_reflection import MemoryReflection
from astrbot_plugin_livingmemory.core.managers.conversation_manager import ConversationManager
from astrbot_plugin_livingmemory.core.models.conversation_models import Message
from astrbot_plugin_livingmemory.core.processors import memory_processor as processor
from astrbot_plugin_livingmemory.core.prompts import prompt_manager
from astrbot_plugin_livingmemory.core.summary_recovery import SummaryRecovery
from astrbot_plugin_livingmemory.core.summary_schedule import SHANGHAI
from tests.test_memory_v04_review import store  # noqa: F401
from tests.test_recall_reliability import engine  # noqa: F401

DAY = int(datetime(2026, 11, 4, tzinfo=SHANGHAI).timestamp())
EDGES = [0, 8*3600, 13*3600, 18*3600+1800, 86400]


class QwenTextCounter:
    """Count ordinary NFC text with the locally bundled Qwen byte-pair ranks."""
    def __init__(self, vocab):
        data = vocab.read_bytes()
        self.sha256 = hashlib.sha256(data).hexdigest()
        self.ranks = {base64.b64decode(key): int(rank)
                      for key, rank in (line.split() for line in data.splitlines())}

    @lru_cache(maxsize=32768)
    def piece_count(self, piece):
        raw = piece.encode("utf8")
        if raw in self.ranks:
            return 1
        parts = [bytes([byte]) for byte in raw]
        while len(parts) > 1:
            candidates = ((self.ranks[pair], i) for i in range(len(parts)-1)
                          if (pair := parts[i]+parts[i+1]) in self.ranks)
            best = min(candidates, default=None)
            if best is None:
                break
            i = best[1]
            parts[i:i+2] = [parts[i] + parts[i+1]]
        return len(parts)

    def count(self, text):
        text = unicodedata.normalize("NFC", text)
        # stdlib re has no \p{L}/\p{N}; expand just the characters in this input.
        chars = set(text)
        letters, numbers = [re.escape("".join(sorted(c for c in chars if
                            unicodedata.category(c).startswith(kind)))) for kind in ("L", "N")]
        letter = "[" + letters + "]" if letters else "(?!)"
        number = "[" + numbers + "]" if numbers else "(?!)"
        pattern = (r"(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n" + letters + numbers + "]?" + letter
                   + "+|" + number + r"| ?[^\s" + letters + numbers
                   + r"]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+")
        pieces = re.findall(pattern, text)
        assert "".join(pieces) == text
        return sum(self.piece_count(piece) for piece in pieces)


def layout(name):
    if name == "all_day":
        return sorted((start + (i+1)*(end-start)//(n+1), label, "user")
                      for start, end in zip(EDGES, EDGES[1:])
                      for label, n in (("busy", 50), ("steady", 15), ("quiet", 1))
                      for i in range(n))
    db = os.environ.get("SUMMARY_COMPARISON_TIMELINE")
    if not db:
        pytest.skip("optional historical metadata layout not supplied")
    aliases = {}
    rows = []
    with sqlite3.connect(Path(db).as_uri()+"?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        for session, stamp, role in conn.execute(
                "SELECT session_id,timestamp,role FROM messages ORDER BY timestamp,id"):
            dt = datetime.fromtimestamp(stamp, SHANGHAI)
            if dt.date().isoformat() == "2026-10-22":
                label = aliases.setdefault(session, f"s{len(aliases)+1}")
                rows.append((dt.hour*3600 + dt.minute*60 + dt.second, label, role))
    assert rows, "historical sample date absent"
    return rows


class FixtureProvider:
    provider_config = {"id": "offline", "model": "fixture"}
    def __init__(self, counter, clock):
        self.counter = counter
        self.clock = clock
        self.arrivals = {}
        self.waits = defaultdict(list)
        self.stats = defaultdict(Counter)

    async def text_chat(self, prompt, system_prompt, **kwargs):
        markers = re.findall(r"评测记录([a-z0-9]+)-(\d+)", prompt)
        assert markers and len({s for s, _ in markers}) == 1
        label = markers[0][0]
        self.waits[label].extend(self.clock.now - self.arrivals[(s, int(n))] for s, n in markers)
        topics = sorted({int(i)//5 for _, i in markers})
        reply = json.dumps({"summary": "项目进度记录。", "topics": ["项目"],
                            "key_facts": [f"工单{n}已登记。" for n in topics],
                            "importance": 0.7}, ensure_ascii=False)
        self.stats[label].update(calls=1, estimated_input=self.counter.count(system_prompt)
                                 + self.counter.count(prompt), estimated_output=self.counter.count(reply))
        return NS(completion_text=reply, is_chunk=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["all_day", "evening"])
@pytest.mark.parametrize("mode", ["rounds", "slots_min_1", "daily_slots"])
async def test_compare(store, engine, tmp_path, monkeypatch, name, mode):
    if "SUMMARY_COMPARISON_VOCAB" not in os.environ:
        pytest.skip("requires a local token vocabulary")
    rows = layout(name)
    counter = QwenTextCounter(Path(os.environ["SUMMARY_COMPARISON_VOCAB"]))
    clock = NS(now=DAY)
    provider = FixtureProvider(counter, clock)
    manager = ConversationManager(store)
    await engine.bind_source_store(store)
    prompts = prompt_manager.PromptManager(str(tmp_path))
    monkeypatch.setattr(prompt_manager, "get_prompt_manager", lambda: prompts)
    monkeypatch.setattr(summary_recovery, "is_session_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(summary_recovery, "is_plugin_enabled_for_session", AsyncMock(return_value=True))
    config = {"reflection_engine.summary_trigger_mode": "rounds" if mode == "rounds" else "daily_slots"}
    if mode == "slots_min_1":
        config["reflection_engine.slot_min_messages"] = 1
    proc = processor.MemoryProcessor(llm_provider=provider)
    reflection = MemoryReflection(None, config, engine, proc, manager, None, set(), set(), asyncio.Lock())
    recovery = SummaryRecovery(reflection)
    monkeypatch.setattr(summary_recovery, "time", NS(time=lambda: clock.now))

    class ReplayDate(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock.now, tz or SHANGHAI)
    monkeypatch.setattr(processor, "datetime", ReplayDate)

    # Poll after each capture and at boundaries. No artificial end-of-day flush for rounds.
    events = [(s, 1, label, role) for s, label, role in rows] + [(s, 0, "", "") for s in EDGES]
    counts = Counter()
    digest = hashlib.sha256()
    for second, is_message, label, role in sorted(events):
        clock.now = DAY + second
        if is_message:
            n = counts[label]
            counts[label] += 1
            provider.arrivals[(label, n)] = clock.now
            session = "offline:GroupMessage:" + label
            body = f"评测记录{label}-{n:04d}：工单{n//5}已登记，等待周五复核。"
            digest.update(json.dumps([second,label,role,body], ensure_ascii=False).encode()+b"\n")
            await store.add_message(Message(
                id=0, session_id=session, role=role, sender_id="member", sender_name="成员",
                group_id=label, platform="offline", timestamp=clock.now, content=body,
                metadata={"source_uid":f"{label}-{n}", "reflection_context":{
                    "persona_id":"p","memory_scope":session}}))
        await recovery.recover_once()

    covered = defaultdict(set)
    for session, payload in await (await store.connection.execute(
            "SELECT session_id,payload FROM summary_jobs WHERE status='completed'")).fetchall():
        job = json.loads(payload)
        label = session.rsplit(":", 1)[1]
        ids = set(range(job["start"], job["end"]))
        assert ids.isdisjoint(covered[label])
        covered[label].update(ids)
    by_session = {}
    for label, n in counts.items():
        cursor = await manager.get_session_metadata("offline:GroupMessage:"+label, "last_summarized_index", 0)
        assert covered[label] == set(range(cursor))
        async with engine.db_connection.execute(
            "SELECT source_uid FROM memory_source_links WHERE session_id=?",
            ("offline:GroupMessage:"+label,),
        ) as link_cursor:
            links = [row[0] for row in await link_cursor.fetchall()]
        assert len(links) == len(set(links)) == cursor
        assert set(links) == {f"{label}-{n}" for n in covered[label]}
        waits = provider.waits[label]
        assert len(waits) == cursor
        by_session[label] = dict(messages=n, covered=cursor, max_wait_seconds=max(waits, default=None),
                                 **provider.stats[label])
    if mode != "rounds":
        assert all(r["covered"] == r["messages"] for r in by_session.values())
    total = {k:sum(r.get(k,0) for r in by_session.values()) for k in
             ("messages","covered","calls","estimated_input","estimated_output")}
    total["max_wait_seconds"] = max(max(v) for v in provider.waits.values() if v)
    assert total["calls"] > 0 and total["covered"] > 0
    result = dict(mode=mode, layout=name, input_sha256=digest.hexdigest(), vocabulary_sha256=counter.sha256,
                  totals=total, sessions=by_session, external_api_calls=0)
    path = Path(os.environ["SUMMARY_COMPARISON_OUTPUT"])/f"comparison-{name}-{mode}.json"
    path.write_text(json.dumps(result, indent=2)+"\n", encoding="utf8")
