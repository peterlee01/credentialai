"""Vercel Python Function backing the credentialai.co live verification demo.

Wraps src.coordinator.run_verification (the same code the CLI uses) with the
HTTP contract the existing frontend (index.html) already expects: POST a
{first_name, last_name, state, profession} JSON body, get back
{verdict_text, summary, facility_matches} on success, 429 once the visitor has used
all their free verifications, or {error} on a bad request or failure.
"""

import hmac
import http.cookies
import json
import os
import sys
import time

# A deployed function's source bundle is read-only; only /tmp is writable.
# Point the audit log there before importing the coordinator (audit.py reads
# this env var at import time). The CLI's own repo-relative log is untouched.
os.environ.setdefault("AUDIT_LOG_PATH", "/tmp/audit_log.jsonl")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from http.server import BaseHTTPRequestHandler  # noqa: E402

from src.coordinator import run_verification  # noqa: E402
from src.tools_facilities import build_facility_matches  # noqa: E402
from src.tools_jobs import add_openings  # noqa: E402
from src.summary import build_summary  # noqa: E402

ALLOWED_STATES = {
    "Arizona", "California", "Colorado", "Florida", "Georgia",
    "Illinois", "New York", "Ohio", "Texas", "Virginia",
}
ALLOWED_PROFESSIONS = {"physician", "psychologist", "counselor", "social worker"}

RUNS_PER_VISITOR = 3

COOKIE_NAME = "cv_used"  # holds how many runs this browser has used
COOKIE_MAX_AGE = 60 * 60 * 24 * 180  # 180 days

# Best-effort, in-memory per-warm-instance limiter — matches what the privacy
# policy already describes ("your IP address, used briefly and only in
# server memory"). Not durable across cold starts; the cookie is the real
# per-visitor limiter.
_seen_ips = {}  # IP -> runs used
_seen_ips_reset_at = time.time()
_SEEN_IPS_TTL = 60 * 60  # clear the in-memory set hourly to bound its size


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        global _seen_ips_reset_at

        if time.time() - _seen_ips_reset_at > _SEEN_IPS_TTL:
            _seen_ips.clear()
            _seen_ips_reset_at = time.time()

        # Owner pass: a request carrying the DEMO_BYPASS_KEY secret skips the per-visitor limit
        # and isn't counted against it.
        bypass_key = os.environ.get("DEMO_BYPASS_KEY", "")
        owner = bool(bypass_key) and hmac.compare_digest(
            self.headers.get("X-Demo-Key", "").encode(), bypass_key.encode())

        cookies = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        client_ip = self.headers.get("x-forwarded-for", self.client_address[0])
        try:
            cookie_runs = int(cookies[COOKIE_NAME].value) if COOKIE_NAME in cookies else 0
        except ValueError:
            cookie_runs = RUNS_PER_VISITOR  # an unreadable count is treated as used up
        runs_used = max(cookie_runs, _seen_ips.get(client_ip, 0))
        if runs_used >= RUNS_PER_VISITOR and not owner:
            self._send_json(429, {})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send_json(400, {"error": "Malformed request body."})
            return

        first_name = str(body.get("first_name") or "").strip()
        last_name = str(body.get("last_name") or "").strip()
        state = str(body.get("state") or "").strip()
        profession = str(body.get("profession") or "").strip().lower()

        if not first_name or not last_name:
            self._send_json(400, {"error": "First and last name are required."})
            return
        if state not in ALLOWED_STATES:
            self._send_json(400, {"error": "Unsupported state."})
            return
        if profession not in ALLOWED_PROFESSIONS:
            self._send_json(400, {"error": "Unsupported profession."})
            return

        try:
            verdict_text, tool_calls = run_verification(first_name, last_name, state, profession)
        except Exception:
            self._send_json(500, {"error": "Something went wrong running verification."})
            return

        try:
            facility_matches = build_facility_matches(tool_calls)
        except Exception:
            facility_matches = None

        openings_notes, nearby = [], []
        try:
            facility_matches, nearby = add_openings(facility_matches, profession, openings_notes)
        except Exception as exc:
            openings_notes.append(f"lookup crashed ({exc.__class__.__name__})")  # matches still show

        try:
            summary = build_summary(first_name, last_name, state, profession, tool_calls)
        except Exception:
            summary = None  # the page falls back to the verdict text alone

        payload = {
            "verdict_text": verdict_text,
            "summary": summary,
            "facility_matches": facility_matches,
            "nearby_openings": nearby,
        }
        if owner:
            # The owner also sees what the job search found, to tune matching.
            self._send_json(200, {**payload, "runs_left": None, "openings_debug": openings_notes})
            return
        runs_used += 1
        _seen_ips[client_ip] = runs_used
        self._send_json(200, {**payload, "runs_left": RUNS_PER_VISITOR - runs_used}, runs_used=runs_used)

    def _send_json(self, status: int, payload: dict, runs_used: int = 0):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if runs_used:
            self.send_header(
                "Set-Cookie",
                f"{COOKIE_NAME}={runs_used}; Max-Age={COOKIE_MAX_AGE}; Path=/; HttpOnly; Secure; SameSite=Lax",
            )
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))
