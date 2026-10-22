"""Vercel Python Function backing the credentialai.co live verification demo.

Wraps src.coordinator.run_verification (the same code the CLI uses) with the
HTTP contract the home page (index.html) expects: POST a
{first_name, last_name, state, profession} JSON body, get back
{verdict_text, summary, report, physician_matches} on success, 429 once the visitor
has used all their free verifications, or {error} on a bad request or failure.

physician_matches are AI-matched physicians similar to the one checked (same
profession and specialty near their practice city, src/clinician_match.py). They
are found and checked while the verification runs.
"""

import json
import os
import sys

# A deployed function's source bundle is read-only; only /tmp is writable.
# Point the audit log there before importing the coordinator (audit.py reads
# this env var at import time). The CLI's own repo-relative log is untouched.
os.environ.setdefault("AUDIT_LOG_PATH", "/tmp/audit_log.jsonl")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from http.server import BaseHTTPRequestHandler  # noqa: E402

from src import visitor_limit  # noqa: E402
from src.check_and_match import check_and_match, validate  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # Free demo limit and owner pass (src/visitor_limit.py). Signed-in facilities use api/facility.py.
        visitor = visitor_limit.check(self.headers, self.client_address)
        if not visitor["allowed"]:
            self._send_json(429, {})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send_json(400, {"error": "Malformed request body."})
            return

        fields = validate(body)
        if isinstance(fields, str):
            self._send_json(400, {"error": fields})
            return
        try:
            payload = check_and_match(*fields)
        except Exception as exc:
            print(f"[verify] failed ({exc!r})")
            self._send_json(500, {"error": "Something went wrong running verification."})
            return

        if visitor["owner"]:
            self._send_json(200, {**payload, "runs_left": None})
            return
        runs_used = visitor_limit.record_run(visitor)
        self._send_json(200, {**payload, "runs_left": visitor_limit.RUNS_PER_VISITOR - runs_used}, runs_used=runs_used)

    def _send_json(self, status: int, payload: dict, runs_used: int = 0):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if runs_used:
            self.send_header("Set-Cookie", visitor_limit.cookie_header(runs_used))
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))
