"""Vercel Python Function for facility accounts (Phase 1: early access).

GET  /api/facility                 -> sign-in config, and (with a Supabase access token in
                                      "Authorization: Bearer ...") the facility, its usage and
                                      its recent verifications
POST /api/facility {"action": ...} -> register | verify | report | intro | delete

A facility signs in with an emailed link (Supabase Auth, the same project as
physician accounts), registers its details, and runs the same verification and
AI matching as the home page demo (src/check_and_match.py). Until you approve
it (facilities.approved in Supabase) it gets FREE_RUNS verifications in total;
once approved, monthly_limit a calendar month. Every run is saved, so the
facility can reopen past results and their PDFs.
"""

import json
import os
import sys
from datetime import datetime, timezone

os.environ.setdefault("AUDIT_LOG_PATH", "/tmp/audit_log.jsonl")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from http.server import BaseHTTPRequestHandler  # noqa: E402

import requests  # noqa: E402

from src import supa  # noqa: E402
from src.check_and_match import check_and_match, report_for, validate  # noqa: E402
from src.tools_facilities import STATE_CODES  # noqa: E402

FREE_RUNS = 3
HISTORY = 25
FACILITY_TYPES = ("Hospital", "Health system", "Group practice", "Clinic", "Staffing agency", "Health plan", "Other")
OWNER_EMAIL = "info@credentialai.co"


def _clip(value, n=120):
    return " ".join(str(value or "").split())[:n]


def _notify_owner(subject: str, text: str):
    """Email the owner through Resend when RESEND_API_KEY is set; otherwise the row in
    Supabase is the record."""
    key = os.environ.get("RESEND_API_KEY")
    if not key:
        return
    try:
        requests.post("https://api.resend.com/emails", timeout=10, headers={"Authorization": f"Bearer {key}"},
                      json={"from": "Credentialai <noreply@credentialai.co>", "to": [OWNER_EMAIL],
                            "subject": subject, "text": text})
    except requests.RequestException as exc:
        print(f"[facility] owner email failed ({exc!r})")


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

    def _facility(self, user_id):
        rows = supa.select("facilities", {"id": f"eq.{user_id}", "select": "*"})
        return rows[0] if rows else None

    def _usage(self, facility):
        """{"used", "limit", "left", "period"}: free runs in total until approved, then per month."""
        params = {"facility_id": f"eq.{facility['id']}", "select": "id"}
        if facility.get("approved"):
            start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            params["created_at"] = f"gte.{start.isoformat()}"
            limit, period = int(facility.get("monthly_limit") or 0), "month"
        else:
            limit, period = FREE_RUNS, "free"
        used = len(supa.select("facility_runs", params))
        return {"used": used, "limit": limit, "left": max(0, limit - used), "period": period}

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
        facility = self._facility(user["id"])
        if not facility:
            self._send(200, {"configured": True, **config, "user": user, "facility": None,
                             "facility_types": FACILITY_TYPES})
            return
        runs = supa.select("facility_runs", {"facility_id": f"eq.{user['id']}", "order": "created_at.desc",
                                             "limit": str(HISTORY), "select": "*"})
        intros = supa.select("intro_requests", {"facility_id": f"eq.{user['id']}", "select": "physician_npi,run_id"})
        self._send(200, {"configured": True, **config, "user": user, "facility": facility,
                         "usage": self._usage(facility), "runs": runs, "intros": intros})

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
            self._send(503, {"error": "Facility accounts aren't set up yet."})
            return
        if not user:
            self._send(401, {"error": "Please sign in again."})
            return
        action = body.get("action")
        try:
            if action == "register":
                self._register(user, body)
                return
            facility = self._facility(user["id"])
            if not facility:
                self._send(400, {"error": "Add your facility details first."})
            elif action == "verify":
                self._verify(facility, body)
            elif action == "report":
                self._report(facility, body)
            elif action == "intro":
                self._intro(facility, body)
            elif action == "delete":
                self._delete(user)
            else:
                self._send(400, {"error": "Unknown action."})
        except requests.HTTPError as exc:
            print(f"[facility] Supabase error {exc!r}")
            self._send(502, {"error": "Our account service had a problem. Please try again."})

    def _register(self, user, body):
        name = _clip(body.get("name"))
        state = str(body.get("state") or "").strip()
        if not name:
            self._send(400, {"error": "Enter your facility's name."})
            return
        facility_type = body.get("facility_type") if body.get("facility_type") in FACILITY_TYPES else "Other"
        existing = self._facility(user["id"])
        row = {"id": user["id"], "email": user["email"], "name": name, "facility_type": facility_type,
               "contact_name": _clip(body.get("contact_name")), "city": _clip(body.get("city"), 60),
               "state": state if state in STATE_CODES else None,
               "updated_at": datetime.now(timezone.utc).isoformat()}
        facility = supa.upsert("facilities", row)
        if not existing:
            print(f"[facility] new sign-up {user['id']}")
            _notify_owner(f"New facility sign-up: {name}",
                          f"{name} ({facility_type}, {row['city'] or '?'}, {row['state'] or '?'}) signed up for "
                          f"early access. Contact: {row['contact_name'] or '?'} <{user['email']}>.\n\n"
                          "Approve it in Supabase: Table Editor -> facilities -> approved = true.")
        self._send(200, {"facility": facility})

    def _verify(self, facility, body):
        usage = self._usage(facility)
        if usage["left"] <= 0:
            self._send(429, {"error": "You've used this month's verifications." if usage["period"] == "month"
                             else "You've used your free verifications. We'll be in touch about early access.",
                             "usage": usage})
            return
        fields = validate(body)
        if isinstance(fields, str):
            self._send(400, {"error": fields})
            return
        first, last, state, profession = fields
        try:
            result = check_and_match(first, last, state, profession)
        except Exception as exc:
            print(f"[facility] verification failed ({exc!r})")
            self._send(500, {"error": "Something went wrong running verification."})
            return
        matches = result.get("physician_matches")
        stored_matches = None
        if matches:
            # Match PDFs are large; history keeps the matches and rebuilds only the main report.
            stored_matches = {**matches, "matches": [{k: v for k, v in m.items() if k != "report"}
                                                     for m in matches.get("matches", [])]}
        report = result.get("report") or {}
        saved = supa.insert("facility_runs", {
            "facility_id": facility["id"], "first_name": first, "last_name": last, "state": state,
            "profession": profession, "verdict": (result["verdict_text"] or "").strip().split("\n")[0].strip().upper(),
            "verdict_text": result["verdict_text"], "summary": result["summary"],
            "physician_matches": stored_matches, "report_id": report.get("id"),
        })
        usage["used"] += 1
        usage["left"] = max(0, usage["limit"] - usage["used"])
        self._send(200, {**result, "run": {k: saved.get(k) for k in ("id", "created_at")} if saved else None,
                         "usage": usage})

    def _report(self, facility, body):
        rows = supa.select("facility_runs", {"id": f"eq.{body.get('run_id')}", "facility_id": f"eq.{facility['id']}",
                                             "select": "*"})
        if not rows:
            self._send(404, {"error": "That verification wasn't found."})
            return
        row = rows[0]
        try:
            generated_at = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            generated_at = None
        report = report_for(row["first_name"], row["last_name"], row["state"], row["profession"], row["verdict_text"],
                            row["summary"], report_id=row.get("report_id"), generated_at=generated_at)
        if not report:
            self._send(500, {"error": "The report couldn't be created."})
            return
        self._send(200, {"report": report})

    def _intro(self, facility, body):
        npi = "".join(ch for ch in str(body.get("npi") or "") if ch.isdigit())[:10]
        if not npi:
            self._send(400, {"error": "Missing the physician's NPI."})
            return
        existing = supa.select("intro_requests", {"facility_id": f"eq.{facility['id']}", "physician_npi": f"eq.{npi}",
                                                  "select": "id"})
        if not existing:
            run_id = body.get("run_id")
            if run_id and not supa.select("facility_runs", {"id": f"eq.{run_id}", "facility_id": f"eq.{facility['id']}",
                                                            "select": "id"}):
                run_id = None
            name, note = _clip(body.get("name")), _clip(body.get("note"), 500)
            supa.insert("intro_requests", {"facility_id": facility["id"], "run_id": run_id, "physician_npi": npi,
                                           "physician_name": name, "note": note})
            _notify_owner(f"Intro request: {name or npi} for {facility['name']}",
                          f"{facility['name']} ({facility.get('contact_name') or '?'} <{facility.get('email')}>) asked "
                          f"for an intro to {name or '?'} (NPI {npi}).\n\nNote: {note or '-'}")
        self._send(200, {"requested": True})

    def _delete(self, user):
        supa.delete_rows("facilities", {"id": user["id"]})  # runs and intro requests go with it
        if not supa.select("profiles", {"id": f"eq.{user['id']}", "select": "id"}):
            supa.delete_user(user["id"])  # no physician profile on this login either
        self._send(200, {"deleted": True})
