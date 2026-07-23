"""Human-in-the-loop review queue.

Scans the real audit trail for unresolved NEEDS REVIEW verifications and lets
a reviewer record a real approve/deny decision with a required rationale,
appended to resolutions.jsonl. This is the human-escalation half of the
GO / NEEDS REVIEW / NO-GO trust pattern — Claude flags ambiguity, a person
resolves it, and the resolution is itself part of the durable record.
"""

import json
import os
import time

AUDIT_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "audit_log.jsonl")
RESOLUTIONS_PATH = os.path.join(os.path.dirname(__file__), "..", "resolutions.jsonl")


def _load_jsonl(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _verdict_keyword(verdict_text: str) -> str:
    first_line = verdict_text.strip().splitlines()[0].strip().upper()
    if first_line in ("GO", "NEEDS REVIEW", "NO-GO"):
        return first_line
    return "UNKNOWN"  # model didn't follow the required first-line format


def get_unresolved_needs_review() -> list:
    audit_entries = _load_jsonl(AUDIT_LOG_PATH)
    resolved_timestamps = {r["resolved_timestamp"] for r in _load_jsonl(RESOLUTIONS_PATH)}
    return [
        entry for entry in audit_entries
        if _verdict_keyword(entry["verdict_text"]) == "NEEDS REVIEW"
        and entry["timestamp"] not in resolved_timestamps
    ]


def resolve(entry: dict, decision: str, rationale: str) -> dict:
    record = {
        "resolved_timestamp": entry["timestamp"],
        "request": entry["request"],
        "decision": decision,
        "rationale": rationale,
        "resolved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    with open(RESOLUTIONS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    return record
