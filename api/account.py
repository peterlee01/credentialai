"""Vercel Python Function for physician accounts (Phase 1).

GET  /api/account                      -> sign-in config, and (with a Supabase access token
                                          in "Authorization: Bearer ...") the profile, latest
                                          verification and runs left today
POST /api/account {"action": ...}      -> npi_search | claim | prefs | verify | report | delete

Sign-in itself happens in the browser with Supabase Auth (email links); this
function checks the token with Supabase on every request and only ever reads
or writes the signed-in physician's own rows.
"""

import base64
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault("AUDIT_LOG_PATH", "/tmp/audit_log.jsonl")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from http.server import BaseHTTPRequestHandler  # noqa: E402

from src import supa  # noqa: E402
from src.coordinator import run_verification  # noqa: E402
from src.prefs import clean as clean_prefs  # noqa: E402
from src.report import build_report_pdf  # noqa: E402
from src.summary import build_summary  # noqa: E402
from src.tools_facilities import SEARCH_PREFS, STATE_CODES, build_facility_matches  # noqa: E402
from src.tools_npi import get_npi_identity  # noqa: E402

ALLOWED_PROFESSIONS = {"physician", "psychologist", "counselor", "social worker"}
RUNS_PER_DAY = 3


def _npi_records(first_name, last_name, state, profession, npi=""):
    result = json.loads(get_npi_identity.call({
        "first_name": first_name, "last_name": last_name, "state": STATE_CODES.get(state, state),
        "profession": profession, "npi": npi,
    }))
    return result.get("matches") or []


class handler(BaseHTTPRequestHandler):
    # ---------- plumbing ----------
    def _send(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _user(self):
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        return supa.user_from_token(token) if token else None

    def _profile(self, user_id):
        rows = supa.select("profiles", {"id": f"eq.{user_id}", "select": "*"})
        return rows[0] if rows else None

    def _runs_left(self, user_id):
        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        rows = supa.select("verifications", {"user_id": f"eq.{user_id}", "created_at": f"gte.{since}",
                                             "select": "id"})
        return max(0, RUNS_PER_DAY - len(rows))

    # ---------- GET ----------
    def do_GET(self):
        config = supa.public_config()
        if not config:
            self._send(200, {"configured": False})
            return
        try:
            user = self._user()
        except supa.NotConfigured:
            self._send(200, {"configured": False})
            return
        if not user:
            self._send(200, {"configured": True, **config, "user": None})
            return
        profile = self._profile(user["id"])
        latest = supa.select("verifications", {"user_id": f"eq.{user['id']}", "order": "created_at.desc",
                                               "limit": "1", "select": "*"})
        self._send(200, {"configured": True, **config, "user": user, "profile": profile,
                         "latest": latest[0] if latest else None,
                         "runs_left": self._runs_left(user["id"]) if profile else None})

    # ---------- POST ----------
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"error": "Malformed request body."})
            return
        try:
            user = self._user()
        except supa.NotConfigured:
            self._send(503, {"error": "Accounts aren't set up yet."})
            return
        if not user:
            self._send(401, {"error": "Please sign in again."})
            return
        action = body.get("action")
        try:
            if action == "npi_search":
                self._npi_search(body)
            elif action == "claim":
                self._claim(user, body)
            elif action == "prefs":
                self._prefs(user, body)
            elif action == "verify":
                self._verify(user)
            elif action == "report":
                self._report(user)
            elif action == "delete":
                supa.delete_user(user["id"])
                self._send(200, {"deleted": True})
            else:
                self._send(400, {"error": "Unknown action."})
        except Exception as exc:
            print(f"[account] {action} failed: {exc.__class__.__name__}: {exc}")
            self._send(500, {"error": "Something went wrong. Please try again."})

    def _fields(self, body):
        first = str(body.get("first_name") or "").strip()
        last = str(body.get("last_name") or "").strip()
        state = str(body.get("state") or "").strip()
        profession = str(body.get("profession") or "").strip().lower()
        if not first or not last or state not in STATE_CODES or profession not in ALLOWED_PROFESSIONS:
            return None
        return first, last, state, profession

    def _npi_search(self, body):
        fields = self._fields(body)
        if not fields:
            self._send(400, {"error": "Enter your name, state and profession."})
            return
        records = _npi_records(*fields)
        npis = [r["npi"] for r in records if r.get("npi")]
        claimed = set()
        if npis:
            rows = supa.select("profiles", {"npi": f"in.({','.join(npis)})", "select": "npi"})
            claimed = {r["npi"] for r in rows}
        self._send(200, {"records": [{**r, "claimed": r.get("npi") in claimed} for r in records]})

    def _claim(self, user, body):
        fields = self._fields(body)
        npi = "".join(ch for ch in str(body.get("npi") or "") if ch.isdigit())
        if not fields or len(npi) != 10:
            self._send(400, {"error": "Choose your NPI record."})
            return
        if body.get("attest") is not True:
            self._send(400, {"error": "Please confirm this record is you."})
            return
        first, last, state, profession = fields
        # Re-check the NPI with the registry: it must exist, fit the profession and carry this last name.
        records = _npi_records(first, last, state, profession, npi=npi)
        record = next((r for r in records if (r.get("last_name") or "").lower() == last.lower()), None)
        if not record:
            self._send(400, {"error": "That NPI doesn't match your name and profession in the registry."})
            return
        taken = supa.select("profiles", {"npi": f"eq.{npi}", "id": f"neq.{user['id']}", "select": "id"})
        if taken:
            self._send(409, {"error": "This NPI is already linked to another account. Contact us if it's yours."})
            return
        profile = supa.upsert("profiles", {
            "id": user["id"], "email": user.get("email"), "first_name": first, "last_name": last,
            "state": state, "profession": profession, "npi": npi,
            "specialty": record.get("primary_taxonomy_desc"),
            "practice_city": (record.get("practice_city") or "").title() or None,
            "attested_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        self._send(200, {"profile": profile, "runs_left": self._runs_left(user["id"])})

    def _prefs(self, user, body):
        if not self._profile(user["id"]):
            self._send(400, {"error": "Claim your NPI first."})
            return
        profile = supa.update("profiles", {"id": user["id"]}, {
            "prefs": clean_prefs(body.get("prefs")), "updated_at": datetime.now(timezone.utc).isoformat()})
        self._send(200, {"profile": profile})

    def _report(self, user):
        """PDF of the latest saved verification, rebuilt on the server from the stored result,
        so a physician can download it any time without using a run."""
        profile = self._profile(user["id"])
        latest = supa.select("verifications", {"user_id": f"eq.{user['id']}", "order": "created_at.desc",
                                               "limit": "1", "select": "*"})
        if not profile or not latest:
            self._send(400, {"error": "Run your first verification to create a report."})
            return
        row = latest[0]
        report_id = row.get("report_id") or uuid.uuid4().hex[:12].upper()
        try:
            generated_at = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            generated_at = None
        pdf = build_report_pdf(profile["first_name"], profile["last_name"], profile["state"], profile["profession"],
                               row.get("verdict_text"), row.get("summary"), row.get("matches"),
                               report_id=report_id, generated_at=generated_at)
        self._send(200, {"report": {"id": report_id,
                                    "filename": f"credentialai-report-{profile['last_name']}-{report_id}.pdf".lower(),
                                    "pdf_base64": base64.b64encode(pdf).decode("ascii")}})

    def _verify(self, user):
        profile = self._profile(user["id"])
        if not profile:
            self._send(400, {"error": "Claim your NPI first."})
            return
        runs_left = self._runs_left(user["id"])
        if runs_left <= 0:
            self._send(429, {"error": f"You've used your {RUNS_PER_DAY} verifications for today."})
            return
        first, last, state, profession = (profile["first_name"], profile["last_name"], profile["state"],
                                          profile["profession"])
        prefs_token = SEARCH_PREFS.set(clean_prefs(profile.get("prefs")))
        try:
            verdict_text, tool_calls = run_verification(first, last, state, profession, npi=profile["npi"])
        finally:
            SEARCH_PREFS.reset(prefs_token)
        summary = build_summary(first, last, state, profession, tool_calls)
        notes = []
        try:
            # Up to 10: the page shows 5 and "Search more jobs" reveals the rest.
            matches = build_facility_matches(tool_calls, limit=10, notes=notes, prefs=profile.get("prefs"))
        except Exception as exc:
            matches = None
            notes.append(f"matching crashed ({exc!r})")
        if matches is None and not any(t["name"] == "match_facilities" for t in tool_calls):
            notes.append("matching didn't run: the verification called "
                         + (", ".join(t["name"] for t in tool_calls) or "no tools"))
        for note in notes:
            print(f"[matching] {note}")
        report_id = uuid.uuid4().hex[:12].upper()
        report = None
        try:
            pdf = build_report_pdf(first, last, state, profession, verdict_text, summary, matches,
                                   report_id=report_id)
            report = {"id": report_id, "filename": f"credentialai-report-{last}-{report_id}.pdf".lower(),
                      "pdf_base64": base64.b64encode(pdf).decode("ascii")}
        except Exception as exc:
            print(f"[report] failed ({exc.__class__.__name__})")
        verdict = (verdict_text or "").strip().split("\n")[0].strip().upper()
        saved = supa.insert("verifications", {
            "user_id": user["id"], "verdict": verdict, "verdict_text": verdict_text,
            "summary": summary, "matches": matches, "report_id": report_id if report else None,
        })
        print(f"[account] verification {saved and saved.get('id')} report {report_id} for user {user['id']}")
        self._send(200, {"verdict_text": verdict_text, "summary": summary, "facility_matches": matches,
                         "report": report, "runs_left": runs_left - 1})
