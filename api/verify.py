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

import base64
import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

# A deployed function's source bundle is read-only; only /tmp is writable.
# Point the audit log there before importing the coordinator (audit.py reads
# this env var at import time). The CLI's own repo-relative log is untouched.
os.environ.setdefault("AUDIT_LOG_PATH", "/tmp/audit_log.jsonl")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from http.server import BaseHTTPRequestHandler  # noqa: E402

from src import visitor_limit  # noqa: E402
from src.clinician_match import match_similar  # noqa: E402
from src.coordinator import run_verification  # noqa: E402
from src.tools_facilities import STATE_CODES  # noqa: E402
from src.tools_npi import get_npi_identity  # noqa: E402
from src.summary import build_summary  # noqa: E402
from src.report import build_report_pdf  # noqa: E402

ALLOWED_STATES = {
    "Alabama", "Alaska", "Arizona", "California", "Colorado", "Connecticut", "Delaware", "Florida",
    "Georgia", "Illinois", "Maine", "Maryland", "Massachusetts", "Michigan", "Missouri",
    "New Hampshire", "New Jersey", "New York", "Ohio", "Rhode Island", "Tennessee", "Texas",
    "Vermont", "Virginia", "Washington", "West Virginia",
}
ALLOWED_PROFESSIONS = {"physician", "psychologist", "counselor", "social worker"}



class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # The per-visitor limit (and owner pass) is shared with /api/match.
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

        def similar_physicians():
            identity = json.loads(get_npi_identity.call({
                "first_name": first_name, "last_name": last_name,
                "state": STATE_CODES.get(state, state), "profession": profession}))
            return match_similar(identity, state, profession)

        with ThreadPoolExecutor(max_workers=2) as pool:
            matching = pool.submit(similar_physicians)
            try:
                verdict_text, tool_calls = run_verification(first_name, last_name, state, profession,
                                                            match_facilities_after=False)
            except Exception:
                self._send_json(500, {"error": "Something went wrong running verification."})
                return
            try:
                physician_matches = matching.result()
            except Exception as exc:
                physician_matches = None
                print(f"[clinicians] matching failed ({exc!r})")

        try:
            summary = build_summary(first_name, last_name, state, profession, tool_calls)
        except Exception:
            summary = None  # the page falls back to the verdict text alone

        report = None
        try:
            report_id = uuid.uuid4().hex[:12].upper()
            pdf = build_report_pdf(first_name, last_name, state, profession, verdict_text, summary,
                                   report_id=report_id)
            report = {"id": report_id, "filename": f"credentialai-report-{last_name}-{report_id}.pdf".lower(),
                      "pdf_base64": base64.b64encode(pdf).decode("ascii")}
            print(f"[report] {report_id} for {first_name} {last_name} ({profession}, {state})")
        except Exception as exc:
            print(f"[report] failed ({exc.__class__.__name__})")  # the result still shows without a report

        payload = {
            "verdict_text": verdict_text,
            "summary": summary,
            "physician_matches": physician_matches,
            "report": report,
        }
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
