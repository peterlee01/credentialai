"""Backup license check through License Verify (licenseverificationapi.com), a paid
API built from state board rosters, for states without a free public source.
It looks up one license by profession, state and license number (all required);
the license number comes from the NPPES registry.

Only runs when LICENSE_VERIFY_API_KEY is set. The vendor's full reference is
behind sign-in, so responses are read without relying on exact field names:
the record list, name, status, expiration, license type and number are
recognised by their keys. A response that can't be read comes back
"unverified" with the keys it had, so the mapping can be fixed from one run.
Results are cached per warm instance for a day to stay inside the free tier.
"""

import json
import os
import re
import time

import requests

DEFAULT_URL = "https://licenseverificationapi.com/api/v1/verify"
CACHE_TTL_SECONDS = 24 * 60 * 60
_cache = {}

# How the vendor names each profession.
_PROFESSIONS = {"physician": "physician"}

# States each profession is covered in, from License Verify's coverage matrix
# (licenseverificationapi.com/coverage, October 2026). It has no psychologist, counselor or
# social worker rows. Update this when their coverage grows.
COVERAGE = {
    "physician": {"AK", "AL", "CA", "CO", "CT", "DE", "FL", "IL", "MA", "MD", "ME", "MI", "MO", "NH", "NJ", "NY",
                  "OH", "RI", "TN", "TX", "VT", "WA", "WV"},
}


def configured() -> bool:
    return bool(os.environ.get("LICENSE_VERIFY_API_KEY"))


def covers(profession: str, state_code: str) -> bool:
    return state_code.upper() in COVERAGE.get(profession, set())


def _key(record: dict, *patterns) -> str:
    """The first key matching one of the patterns, exactly or after a nesting prefix
    (e.g. "license_status" for "status")."""
    for prefix in ("", r"(?:\w+_)?"):
        for pattern in patterns:
            for key in record:
                if re.fullmatch(prefix + pattern, key.lower()):
                    return key
    return ""


def _flatten(record: dict, prefix: str = "") -> dict:
    """Nested objects become prefixed keys, e.g. {"license": {"status": ...}} -> "license_status"."""
    flat = {}
    for key, value in record.items():
        name = f"{prefix}_{key}" if prefix else str(key)
        if isinstance(value, dict):
            flat.update(_flatten(value, name))
        else:
            flat[name] = value
    return flat


def _words(value) -> list:
    return re.findall(r"[a-z]+", str(value or "").lower())


_RESULT_LISTS = ("results", "data", "licenses", "matches", "records", "items", "providers", "licensees")
_WRAPPERS = ("data", "result", "record", "verification")


def _records(data):
    """(records, recognised) for a response. Records come from a results list under a usual key,
    a wrapper object such as {"data": {...}}, or the object itself when it has a status. Other
    lists (e.g. an empty list of disciplinary actions) are never taken for the results."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)], True
    if not isinstance(data, dict):
        return [], False
    for key in _RESULT_LISTS:
        if isinstance(data.get(key), list):
            return [r for r in data[key] if isinstance(r, dict)], True
    for key in _WRAPPERS:
        if isinstance(data.get(key), dict):
            records, recognised = _records(data[key])
            if recognised:
                return records, True
    flat = {k: v for k, v in _flatten(data).items() if not isinstance(v, list)}
    if _key(flat, r"status", r"license_?status", r"standing", r"\w*_status"):
        return [data], True
    lists = [v for v in data.values() if isinstance(v, list) and v and all(isinstance(r, dict) for r in v)]
    if len(lists) == 1:
        return lists[0], True
    return [], False


def _describe(data) -> str:
    """Field names, with short plain values, so an unexpected format can be read from the note."""
    if not isinstance(data, dict):
        return f"a {type(data).__name__}: {str(data)[:80]}"
    parts = []
    for key, value in _flatten(data).items():
        if isinstance(value, (bool, int, float)) or value is None or (isinstance(value, str) and len(value) <= 60):
            parts.append(f"{key}={value!r}")
        else:
            parts.append(f"{key} ({type(value).__name__})")
    return "fields: " + ", ".join(parts[:20])


def _name_fits(record: dict, first: str, last: str) -> bool:
    first_key, last_key = _key(record, r"first_?name"), _key(record, r"last_?name")
    if last_key:
        return (_words(record.get(last_key)) == _words(last)
                and (not first_key or _words(record.get(first_key))[:1] == _words(first)[:1]))
    name_key = "name" if "name" in record else _key(record, r"full_?name", r"licensee_?name", r"holder_?name",
                                                     r"provider_?name")
    if not name_key:
        return None  # the record doesn't say whose license it is
    words = _words(record.get(name_key))
    return all(w in words for w in _words(last) + _words(first)[:1])


def lookup(first_name: str, last_name: str, profession: str, state_code: str, license_number: str = ""):
    """{"status": raw status or None, "records": [...], "note": ...} for one clinician, or a
    dict with only "note" when the lookup couldn't run or be read."""
    api_key = os.environ.get("LICENSE_VERIFY_API_KEY")
    cache_key = (first_name.lower(), last_name.lower(), profession, state_code, license_number)
    cached = _cache.get(cache_key)
    if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    # The API looks up one license by number; the name is checked against what it returns.
    params = {"state": state_code, "profession": _PROFESSIONS[profession],
              "license_number": license_number.strip()}
    try:
        resp = requests.get(os.environ.get("LICENSE_VERIFY_URL") or DEFAULT_URL, params=params, timeout=20,
                            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"[license-vendor] request failed: {exc!r}")
        return {"note": f"License Verify could not be reached ({exc.__class__.__name__})."}
    if resp.status_code == 404:
        out = {"records": []}
    elif resp.status_code != 200:
        message = data.get("error") or data.get("message") if isinstance(data, dict) else None
        print(f"[license-vendor] HTTP {resp.status_code}: {json.dumps(data)[:300]}")
        return {"note": f"License Verify returned an error ({resp.status_code}{': ' + str(message)[:80] if message else ''})."}
    else:
        records, recognised = _records(data)
        if not recognised:
            print(f"[license-vendor] unrecognised response: {json.dumps(data)[:500]}")
            return {"note": f"License Verify's response couldn't be read automatically ({_describe(data)})."}
        out, unnamed = {"records": [], "returned": len(records), "response": _describe(data)}, 0
        if records:
            out["sample"] = _describe(records[0])
        for record in records:
            record = _flatten(record)
            fits = _name_fits(record, first_name, last_name)
            if fits is None and not license_number:
                unnamed += 1
                continue
            if fits is False:
                continue
            status_key = _key(record, r"status", r"license_?status", r"standing", r"\w*_status", r"\w*standing")
            out["records"].append({
                "license_number": record.get(_key(record, r"license_?number", r"license_?no")),
                "license_type": record.get(_key(record, r"license_?type", r"type", r"profession", r"credential")),
                "status_text": str(record.get(status_key) or ""),
                "expiration_date": str(record.get(_key(record, r"expir\w*", r"expires_?\w*", r"valid_?(until|to|through)"))
                                       or "")[:10] or None,
                "board": record.get(_key(record, r"source_?board", r"board", r"source", r"issuing_?\w*")),
                "discipline_on_record": bool(record.get(_key(record, r"disciplin\w*", r"\w*actions?"))),
                "fields": sorted(record)[:25],
            })
        if unnamed and not out["records"]:
            return {"note": "License Verify's records don't name the license holder, so they can't be "
                            "matched to this person."}
    _cache[cache_key] = (time.time(), out)
    return out
