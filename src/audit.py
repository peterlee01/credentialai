"""Append-only audit trail — one JSON line per verification request."""

import json
import os
import time

LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "audit_log.jsonl")


def log_verification(request: dict, verdict_text: str) -> None:
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "request": request,
        "verdict_text": verdict_text,
    }
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
