"""Validate extraction candidates against exact, transport-identified quotations."""
import json
import math

from .memory_source import serialize_source_messages, source_reference
from .source_identity import fingerprint

CLAIM_INSTRUCTION = """
同时输出 claims 数组。每项包含：
assertion（候选主张）、subject_id（明确的稳定账号 ID；无法确定填 null）、
predicate（属性）、value（属性值）、qualifier（self_report/reported/uncertain/
denied/hypothetical/joke/plan）、valid_from/valid_to（有明确依据才填 Unix 秒，否则 null）、
evidence:[{"source_uid":"输入提供的来源 ID","quote":"该条消息中连续、逐字的原文"}]。
不得把群成员名单当作事实主体，不得把转述、玩笑、否认或助手输出写成确认事实。
不确定的指代保留未知；不得自行指定权限、作用域、可信度或覆盖其他人的主张。
"""


def _timestamp(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("invalid claim time")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError("invalid claim time")
    return result


def validate_claims(candidates, sources):
    rows = serialize_source_messages(sources)
    indexed = {source_reference(row)["source_uid"]: row for row in rows if source_reference(row)["source_uid"]}
    identities = {row["sender_id"] for row in rows if row["role"] == "user"}
    valid = []
    for candidate in candidates[:30] if isinstance(candidates, list) else []:
        if not isinstance(candidate, dict):
            continue
        evidence = []
        for item in candidate.get("evidence", []) if isinstance(candidate.get("evidence"), list) else []:
            if not isinstance(item, dict):
                continue
            row = indexed.get(str(item.get("source_uid") or ""))
            quote = item.get("quote")
            if (row is None or row["role"] != "user" or not isinstance(quote, str)
                or not quote.strip() or quote not in row["content"]):
                evidence = []
                break
            evidence.append({**source_reference(row), "quote": quote, "timestamp": row["timestamp"]})
        if not evidence:
            continue
        subject = candidate.get("subject_id")
        subject = str(subject) if subject is not None else None
        if subject not in identities:
            subject = None
        qualifier = str(candidate.get("qualifier") or "uncertain")
        if qualifier not in {"self_report", "reported", "uncertain", "denied", "hypothetical", "joke", "plan"}:
            qualifier = "uncertain"
        reporters = sorted({item["sender_id"] for item in evidence})
        if subject is not None and subject not in reporters:
            if not any(subject in item["quote"] for item in evidence):
                subject = None
        if qualifier == "self_report" and (subject is None or reporters != [subject]):
            qualifier = "reported"
        try:
            start = _timestamp(candidate.get("valid_from"))
            end = _timestamp(candidate.get("valid_to"))
            if start is not None and end is not None and start >= end:
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        claim = {
            "assertion": str(candidate.get("assertion") or "")[:2000],
            "subject_id": subject, "reporter_ids": reporters,
            "predicate": str(candidate.get("predicate") or "statement")[:120],
            "value": str(candidate.get("value") or "")[:1000],
            "qualifier": qualifier, "valid_from": start, "valid_to": end,
            "evidence": evidence, "epistemic_status": "quoted_claim_not_verified",
        }
        claim["claim_id"] = fingerprint(claim)
        valid.append(claim)
    return valid


def fallback_quotations(sources):
    """A missing extraction cannot turn an unsupported paraphrase into a fact."""
    candidates = []
    for row in serialize_source_messages(sources)[-20:]:
        ref = source_reference(row)
        if row["role"] == "user" and ref["source_uid"]:
            candidates.append({
                "assertion": "", "subject_id": None, "qualifier": "uncertain",
                "evidence": [{"source_uid": ref["source_uid"], "quote": row["content"][:1200]}],
            })
    return validate_claims(candidates, sources)


def claim_text(claim):
    # Display the actual quotations. The model's paraphrase stays a candidate,
    # not an independently verified statement or a personal-profile update.
    return "；".join(
        f"报告者 {e['sender_id']} 原话 {json.dumps(e['quote'], ensure_ascii=False)} "
        f"[msg:{e['source_uid']} r{e['revision']}]"
        for e in claim["evidence"]
    ) + f"（主体候选：{claim.get('subject_id') or '未知'}；语气：{claim['qualifier']}；未独立核实）"
