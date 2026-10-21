"""License status check against states' real, free, public license records.

- Colorado: "Professional and Occupational Licenses in Colorado" from the
  Department of Regulatory Agencies (DORA), https://data.colorado.gov/resource/7s5z-vewr.json
- Washington: "Health Care Provider Credential Data" from the Department of
  Health, https://data.wa.gov/resource/qxh8-f4bd.json (refreshed daily)
- Connecticut: "State Licenses and Credentials" from the eLicensing system,
  https://data.ct.gov/resource/ngch-56tr.json (refreshed daily)

All three are Socrata open-data APIs with no key required. There is no free
public license API that covers every state. Other states go to License Verify,
a paid roster API (src/license_vendor.py), when LICENSE_VERIFY_API_KEY is set;
otherwise they come back "unverified", which the coordinator routes to NEEDS
REVIEW for a person to confirm — no license status is ever invented.
"""

import json
import re

import requests
from anthropic import beta_tool

from . import license_vendor
from .tools_facilities import STATE_CODES
from .tools_npi import NPPES_ENDPOINT, _PROFESSION_TAXONOMY_PREFIXES

CO_LICENSE_ENDPOINT = "https://data.colorado.gov/resource/7s5z-vewr.json"
CO_SOURCE = "Colorado DORA professional license records (live, public)"

# Washington and Connecticut are read without relying on exact column names: rows are found with
# Socrata's full-text search ($q) on the last name, and the name, credential type, status,
# expiration and number columns are recognised by their names. A source whose columns can't be
# recognised returns "unverified" rather than guessing.
SOCRATA_STATES = {
    "WA": {
        "name": "Washington",
        "endpoint": "https://data.wa.gov/resource/qxh8-f4bd.json",
        "source": "Washington State Department of Health provider credentials (live, public)",
        "label": "Washington DOH",
        "url": "https://data.wa.gov/resource/qxh8-f4bd",
        "types": {
            "physician": r"physician and surgeon|osteopathic physician",
            "psychologist": r"psychologist",
            "counselor": r"mental health counselor|chemical dependency|substance use disorder professional",
            "social worker": r"social work",
        },
    },
    "CT": {
        "name": "Connecticut",
        "endpoint": "https://data.ct.gov/resource/ngch-56tr.json",
        "source": "Connecticut eLicensing licenses and credentials (live, public)",
        "label": "Connecticut DPH",
        "url": "https://data.ct.gov/resource/ngch-56tr",
        "types": {
            "physician": r"physician|surgeon|osteopath",
            "psychologist": r"psychologist",
            "counselor": r"professional counselor|alcohol and drug counselor",
            "social worker": r"social work",
        },
    },
}
_STATE_NAMES = {"colorado": "CO", "washington": "WA", "connecticut": "CT"}

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
    return json.dumps({"source": CO_SOURCE, "source_label": "Colorado DORA",
                       "source_url": "https://data.colorado.gov/resource/7s5z-vewr",
                       "state": state, "profession": profession, "status": status, **extra})


def _state_code(state: str) -> str:
    state = state.strip()
    return _STATE_NAMES.get(state.lower()) or STATE_CODES.get(state.title(), state.upper())


def _column(row: dict, *patterns) -> str:
    """The first column whose name matches one of the patterns, in pattern order."""
    for pattern in patterns:
        for key in row:
            if re.fullmatch(pattern, key.lower()):
                return key
    return ""


def _words(value) -> list:
    return re.findall(r"[a-z]+", str(value or "").lower())


def _name_fits(row: dict, first: str, last: str) -> bool:
    first_col, last_col = _column(row, r"first_?name"), _column(row, r"last_?name")
    if first_col and last_col:
        return _words(row.get(last_col)) == _words(last) and _words(row.get(first_col))[:1] == _words(first)[:1]
    name_col = _column(row, r"name", r"full_?name", r"licensee_?name")
    words = _words(row.get(name_col)) if name_col else []
    return bool(words) and all(w in words for w in _words(last) + _words(first)[:1])


def _check_socrata(code: str, first_name, last_name, profession, state, license_number) -> str:
    cfg = SOCRATA_STATES[code]

    def result(status, **extra):
        return json.dumps({"source": cfg["source"], "source_label": cfg["label"], "source_url": cfg["url"],
                           "state": state, "profession": profession, "status": status, **extra})

    try:
        resp = requests.get(cfg["endpoint"], params={"$q": f"{first_name.strip()} {last_name.strip()}", "$limit": 500}, timeout=15)
        resp.raise_for_status()
        rows = resp.json()
    except (requests.RequestException, ValueError) as exc:
        return result("unverified", note=f"{cfg['name']} license records could not be reached "
                                         f"({exc.__class__.__name__}); a person needs to confirm license status.")
    if not isinstance(rows, list):
        return result("unverified", note=f"{cfg['name']} license records returned an unexpected response; "
                                         "a person needs to confirm license status.")
    if rows and not _column(rows[0], r"status", r"license_?status", r"credential_?status"):
        print(f"[license] {code} columns not recognised: {sorted(rows[0])}")
        return result("unverified", note=f"{cfg['name']} license records couldn't be read automatically; "
                                         "a person needs to confirm license status.")

    type_pattern = cfg["types"].get(profession)
    candidates = []
    for row in rows:
        if not _name_fits(row, first_name, last_name):
            continue
        type_cols = [k for k in row if re.search(r"credential|license|profession|type", k.lower())
                     and not re.search(r"number|id$|date|status", k.lower())]
        values = [str(row.get(k) or "").strip() for k in type_cols]
        fitting = [v for v in values if type_pattern and re.search(type_pattern, v.lower())]
        if type_pattern and not fitting:
            continue
        candidates.append((row, fitting[0] if fitting else " ".join(values)))

    def number_col(row):
        return _column(row, r"credential_?number", r"license_?number", r"full_?credential_?code", r".*number")

    # The NPPES license number pins down the exact record when it's available.
    wanted_digits = _digits(license_number)
    if wanted_digits:
        exact = [c for c in candidates if _digits(c[0].get(number_col(c[0]))) == wanted_digits]
        if exact:
            candidates = exact
    # One person often holds the same credential as several records (e.g. an old license
    # replaced by a new one); prefer the active record when exactly one is active.
    active = [c for c in candidates if _normalize_status(c[0].get(_column(c[0], r"status"))) == "active"]
    if len(candidates) > 1 and len(active) == 1:
        candidates = active

    if not candidates:
        return result("not_found", match_count=0)

    records = []
    for row, type_text in candidates[:5]:
        status_col = _column(row, r"status", r"license_?status", r"credential_?status")
        reason_col = _column(row, r"status_?reason")
        expiry_col = _column(row, r"expiration_?date", r".*expir.*")
        action_col = _column(row, r"action_?taken", r".*disciplin.*", r".*action.*")
        status_text = " ".join(str(row.get(c) or "").strip() for c in (status_col, reason_col) if c).strip()
        records.append({
            "license_number": row.get(number_col(row)),
            "license_type": type_text.strip()[:80],
            "status_text": status_text,
            "expiration_date": str(row.get(expiry_col) or "")[:10] or None,
            "city": row.get(_column(row, r"city")),
            "discipline_on_record": bool(action_col and str(row.get(action_col) or "").strip().lower()
                                         not in ("", "no", "none", "n", "false")),
        })

    if len(candidates) > 1:
        return result("ambiguous", match_count=len(candidates), records=records)
    return result(_normalize_status(records[0]["status_text"]), match_count=1, records=records)


def _registry_license(first_name, last_name, profession, code) -> str:
    """The state license number the NPPES registry lists for this person in this state, when
    exactly one registry record of the profession matches; "" otherwise."""
    prefixes = _PROFESSION_TAXONOMY_PREFIXES.get(profession, ())
    try:
        resp = requests.get(NPPES_ENDPOINT, params={"version": "2.1", "first_name": first_name.strip(),
                                                    "last_name": last_name.strip(), "state": code, "limit": 20},
                            timeout=15)
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except (requests.RequestException, ValueError):
        return ""
    found = {}
    for r in results:
        numbers = [t.get("license") for t in r.get("taxonomies", [])
                   if (t.get("code") or "").startswith(prefixes) and (t.get("state") or "").upper() == code
                   and t.get("license")]
        if numbers:
            found[r.get("number")] = numbers[0]
    return next(iter(found.values())) if len(found) == 1 else ""


def _check_vendor(code: str, first_name, last_name, profession, state, license_number) -> str:
    def result(status, **extra):
        return json.dumps({"source": "License Verify API (state board rosters)", "source_label": "License Verify",
                           "source_url": "https://licenseverificationapi.com", "state": state,
                           "profession": profession, "status": status, **extra})

    if not license_vendor.covers(profession, code):
        return result("unverified", note=f"No automatic license source covers {profession}s in {state} yet. "
                                         "A person needs to confirm the license with the state board.")
    # License Verify looks licenses up by number only, so the number has to come from the
    # registry: the one the coordinator passed, or the registry's number for this state.
    license_number = _registry_license(first_name, last_name, profession, code) or license_number
    if not license_number:
        return result("unverified", note=f"License Verify looks licenses up by license number, and the national "
                                         f"provider registry doesn't list a {state} license number for this person. "
                                         "A person needs to confirm the license with the state board.")
    found = license_vendor.lookup(first_name, last_name, profession, code, license_number)

    if "records" not in found:
        return result("unverified", note=found["note"] + " A person needs to confirm license status.")
    records = found["records"]
    wanted_digits = _digits(license_number)
    if wanted_digits and len(records) > 1:
        exact = [r for r in records if _digits(str(r.get("license_number") or "")) == wanted_digits]
        records = exact or records
    if not records:
        returned = found.get("returned") or 0
        note = (f"License Verify returned {returned} record{'s' if returned != 1 else ''} for this search, but none "
                f"matched this name ({found.get('sample', '')})." if returned else
                f"License Verify found no {state} {profession} license numbered {license_number}. The number may be "
                "listed differently by the board, so this doesn't mean the person is unlicensed; a person needs "
                "to confirm the license with the state board.")
        return result("not_found", match_count=0, note=note)
    if not records[0]["status_text"].strip():
        print(f"[license-vendor] record without a recognised status: {records[0]['fields']}")
        return result("unverified", note="License Verify's record couldn't be read automatically (fields: "
                                         + ", ".join(records[0]["fields"]) + "). A person needs to confirm license status.")
    for record in records:
        record.pop("fields", None)
    if len(records) > 1:
        return result("ambiguous", match_count=len(records), records=records[:5])
    return result(_normalize_status(records[0]["status_text"]), match_count=1, records=records)


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
    code = _state_code(state)
    if code in SOCRATA_STATES:
        return _check_socrata(code, first_name, last_name, profession, state, license_number)
    if code != "CO" and license_vendor.configured():
        return _check_vendor(code, first_name, last_name, profession, state, license_number)
    if code != "CO":
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
