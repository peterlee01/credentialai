"""One facility check: verify a named physician and find AI-matched similar physicians.

Shared by the free home page demo (api/verify.py) and signed-in facility
accounts (api/facility.py), so both run exactly the same check. Similar
physicians (src/clinician_match.py) are found and checked on a second thread
while the coordinator verifies the named physician.
"""

import base64
import json
import uuid
from concurrent.futures import ThreadPoolExecutor

from .clinician_match import match_similar
from .coordinator import run_verification
from .report import build_report_pdf
from .summary import build_summary
from .tools_facilities import STATE_CODES
from .tools_npi import get_npi_identity

ALLOWED_PROFESSIONS = {"physician", "psychologist", "counselor", "social worker"}


def validate(body: dict):
    """(first_name, last_name, state, profession) from a request body, or a string error."""
    first_name = str(body.get("first_name") or "").strip()
    last_name = str(body.get("last_name") or "").strip()
    state = str(body.get("state") or "").strip()
    profession = str(body.get("profession") or "").strip().lower()
    if not first_name or not last_name:
        return "First and last name are required."
    if state not in STATE_CODES:
        return "Unsupported state."
    if profession not in ALLOWED_PROFESSIONS:
        return "Unsupported profession."
    return first_name, last_name, state, profession


def report_for(first_name, last_name, state, profession, verdict_text, summary, report_id=None, generated_at=None):
    """{"id", "filename", "pdf_base64"} for a verification, or None if the PDF fails."""
    report_id = report_id or uuid.uuid4().hex[:12].upper()
    try:
        pdf = build_report_pdf(first_name, last_name, state, profession, verdict_text, summary,
                               report_id=report_id, generated_at=generated_at)
    except Exception as exc:
        print(f"[report] failed ({exc.__class__.__name__})")  # the result still shows without a report
        return None
    print(f"[report] {report_id} for {first_name} {last_name} ({profession}, {state})")
    return {"id": report_id, "filename": f"credentialai-report-{last_name}-{report_id}.pdf".lower(),
            "pdf_base64": base64.b64encode(pdf).decode("ascii")}


def check_and_match(first_name: str, last_name: str, state: str, profession: str) -> dict:
    """{verdict_text, summary, report, physician_matches}. Raises if the verification itself fails."""
    def similar_physicians():
        identity = json.loads(get_npi_identity.call({
            "first_name": first_name, "last_name": last_name,
            "state": STATE_CODES.get(state, state), "profession": profession}))
        return match_similar(identity, state, profession)

    with ThreadPoolExecutor(max_workers=2) as pool:
        matching = pool.submit(similar_physicians)
        verdict_text, tool_calls = run_verification(first_name, last_name, state, profession,
                                                    match_facilities_after=False)
        try:
            physician_matches = matching.result()
        except Exception as exc:
            physician_matches = None
            print(f"[clinicians] matching failed ({exc!r})")

    try:
        summary = build_summary(first_name, last_name, state, profession, tool_calls)
    except Exception:
        summary = None  # the page falls back to the verdict text alone

    return {
        "verdict_text": verdict_text,
        "summary": summary,
        "physician_matches": physician_matches,
        "report": report_for(first_name, last_name, state, profession, verdict_text, summary),
    }
