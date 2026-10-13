"""License status check against Colorado's real, free, public license records.

Queries the "Professional and Occupational Licenses in Colorado" dataset that
the Colorado Department of Regulatory Agencies (DORA) publishes on the
Colorado Information Marketplace — no API key required. See
https://data.colorado.gov/resource/7s5z-vewr.json

There is no free public license API that covers every state, so only Colorado
is checked live. Any other state comes back "unverified", which the
coordinator routes to NEEDS REVIEW for a person to confirm — no license
status is ever invented.
"""

import json

import requests
from anthropic import beta_tool

CO_LICENSE_ENDPOINT = "https://data.colorado.gov/resource/7s5z-vewr.json"
CO_SOURCE = "Colorado DORA professional license records (live, public)"

# DORA license type codes (the prefix of a Colorado license number, e.g. "PSY.0001234").
_CO_LICENSE_TYPES = {
    "physician": {"DR"},
    "psychologist": {"PSY"},
    "counselor": {"LPC", "LAC"},
    "social worker": {"CSW", "LSW", "SWR"},
}

_EXPIRED = {"expired", "inactive", "lapsed", "closed"}
_REVOKED = {"revoked", "suspended", "surrendered", "relinquished", "denied", "restricted"}
_PENDING = {"pending", "in process", "applicant"}


def _soql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _license_type(row: dict) -> str:
    lic_type = (row.get("licensetype") or "").strip().upper()
    if lic_type:
        return lic_type
    return (row.get("licensenumber") or "").split(".")[0].strip().upper()


def _normalize_status(raw: str) -> str:
    text = (raw or "").strip().lower()
    if text.startswith("active"):
        return "active"
    if any(word in text for word in _REVOKED):
        return "revoked"
    if any(word in text for word in _EXPIRED):
        return "expired"
    if any(word in text for word in _PENDING):
        return "pending"
    return "unverified"


def _digits(value: str) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit()).lstrip("0")


def _result(state, profession, status, **extra) -> str:
    return json.dumps({"source": CO_SOURCE, "state": state, "profession": profession,
                       "status": status, **extra})


@beta_tool
def check_license_status(first_name: str, last_name: str, profession: str, state: str,
                         license_number: str = "") -> str:
    """Look up a clinician's professional license status with the state licensing board.

    Args:
        first_name: Clinician's first name.
        last_name: Clinician's last name.
        profession: One of "psychologist", "counselor", "social worker", "physician".
        state: Full state name (e.g. "Colorado") or two-letter code.
        license_number: The license number from the NPPES identity result, if it has one.
    """
    profession = profession.strip().lower()
    if state.strip().lower() not in {"colorado", "co"}:
        return json.dumps({
            "source": None,
            "state": state,
            "profession": profession,
            "status": "unverified",
            "note": f"No live license source is connected for {state}; "
                    "a person needs to confirm license status with the state board.",
        })

    where = (f"upper(lastname)={_soql_str(last_name.strip().upper())} AND "
             f"upper(firstname)={_soql_str(first_name.strip().upper())}")
    try:
        resp = requests.get(CO_LICENSE_ENDPOINT, params={"$where": where, "$limit": 50}, timeout=15)
        resp.raise_for_status()
        rows = resp.json()
    except (requests.RequestException, ValueError) as exc:
        return _result(state, profession, "unverified",
                       note=f"Colorado license records could not be reached ({exc.__class__.__name__}); "
                            "a person needs to confirm license status.")

    wanted_types = _CO_LICENSE_TYPES.get(profession, set())
    candidates = [r for r in rows if _license_type(r) in wanted_types]

    # The NPPES license number pins down the exact record when it's available.
    wanted_digits = _digits(license_number)
    if wanted_digits:
        exact = [r for r in candidates if _digits(r.get("licensenumber")) == wanted_digits]
        if exact:
            candidates = exact

    if not candidates:
        return _result(state, profession, "not_found", match_count=0)

    records = [{
        "license_number": r.get("licensenumber"),
        "license_type": _license_type(r),
        "status_text": r.get("licensestatusdescription"),
        "expiration_date": (r.get("licenseexpirationdate") or "")[:10] or None,
        "city": r.get("city"),
        "discipline_on_record": bool(r.get("casenumber") or r.get("programaction")),
    } for r in candidates[:5]]

    if len(candidates) > 1:
        return _result(state, profession, "ambiguous", match_count=len(candidates), records=records)

    return _result(state, profession, _normalize_status(records[0]["status_text"]),
                   match_count=1, records=records)
