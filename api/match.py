"""Vercel Python Function for the home page Facilities tab: verified clinicians matched to a
facility's opening.

POST {city, state, profession, setting?, specialty?} -> {matches: [...], ...} with each
match's checks and PDF report, 429 once the visitor has used their free runs (the same
limit as /api/verify), or {error} on a bad request or failure.
"""

import json
import os
import sys

os.environ.setdefault("AUDIT_LOG_PATH", "/tmp/audit_log.jsonl")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from http.server import BaseHTTPRequestHandler  # noqa: E402

from src import visitor_limit  # noqa: E402
from src.clinician_match import PHYSICIAN_SPECIALTIES, SETTINGS, match_clinicians  # noqa: E402
from src.tools_facilities import STATE_CODES  # noqa: E402

ALLOWED_PROFESSIONS = {"physician", "psychologist", "counselor", "social worker"}


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
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

        city = " ".join(str(body.get("city") or "").split())[:60]
        state = str(body.get("state") or "").strip()
        profession = str(body.get("profession") or "").strip().lower()
        setting = str(body.get("setting") or "").strip().lower()
        specialty = str(body.get("specialty") or "").strip()
        if not city:
            self._send_json(400, {"error": "Enter your facility's city."})
            return
        if state not in STATE_CODES:
            self._send_json(400, {"error": "Unsupported state."})
            return
        if profession not in ALLOWED_PROFESSIONS:
            self._send_json(400, {"error": "Unsupported profession."})
            return
        setting = setting if setting in SETTINGS else ""
        specialty = specialty if profession == "physician" and specialty in PHYSICIAN_SPECIALTIES else ""

        try:
            result = match_clinicians(city, state, profession, setting, specialty)
        except Exception as exc:
            print(f"[clinicians] match failed: {exc!r}")
            self._send_json(500, {"error": "Something went wrong finding clinicians."})
            return

        if visitor["owner"]:
            self._send_json(200, {**result, "runs_left": None})
            return
        runs_used = visitor_limit.record_run(visitor)
        self._send_json(200, {**result, "runs_left": visitor_limit.RUNS_PER_VISITOR - runs_used}, runs_used=runs_used)

    def _send_json(self, status: int, payload: dict, runs_used: int = 0):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if runs_used:
            self.send_header("Set-Cookie", visitor_limit.cookie_header(runs_used))
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))
