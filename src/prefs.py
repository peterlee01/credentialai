"""Physician preferences applied to job matches: setting, minimum pay, job type,
contract type, and where to look.

Preferences come from a signed-in physician's profile, e.g.
{"setting": "outpatient", "min_pay": 120000, "employment": "full-time",
 "contract": "locums", "area": "state", "also_cities": ["Seattle, WA"]}.
A listing only drops out when it clearly fails a preference; listings that
don't say (no pay given, no schedule given) are kept. The exception is a
contractor, per diem or locums preference: those roles say so in the posting,
so a listing has to show it. Area and extra cities widen the job searches
(src/tools_facilities.py).
"""

import re

SETTINGS = ("any", "inpatient", "outpatient", "telehealth")
EMPLOYMENT = ("any", "full-time", "part-time")
CONTRACTS = ("any", "employee", "contractor", "per-diem", "locums")
AREAS = ("near", "state")
MAX_ALSO_CITIES = 2
HOURS_PER_YEAR = 2080

_SETTING_WORDS = {
    "inpatient": r"inpatient|hospital|acute|residential|\bunit\b",
    "outpatient": r"outpatient|\bclinics?\b|\bpractice\b|\boffice\b|ambulatory",
    "telehealth": r"telehealth|tele-health|virtual|remote|online",
}


def clean(prefs) -> dict:
    """Keep only known preference values."""
    prefs = prefs or {}
    out = {}
    if prefs.get("setting") in SETTINGS[1:]:
        out["setting"] = prefs["setting"]
    if prefs.get("employment") in EMPLOYMENT[1:]:
        out["employment"] = prefs["employment"]
    try:
        min_pay = int(float(prefs.get("min_pay") or 0))
    except (TypeError, ValueError):
        min_pay = 0
    if 0 < min_pay < 2_000_000:
        out["min_pay"] = min_pay
    if prefs.get("contract") in CONTRACTS[1:]:
        out["contract"] = prefs["contract"]
    if prefs.get("area") in AREAS[1:]:
        out["area"] = prefs["area"]
    cities = []
    for raw in prefs.get("also_cities") or []:
        city = parse_city(raw)
        if city and city not in cities:
            cities.append(city)
    if cities:
        out["also_cities"] = ["%s, %s" % c for c in cities[:MAX_ALSO_CITIES]]
    return out


def parse_city(raw):
    """("Seattle", "WA") from "seattle, wa", or None if it isn't "City, ST"."""
    match = re.fullmatch(r"\s*([A-Za-z][A-Za-z .'-]{0,40}?)\s*,\s*([A-Za-z]{2})\s*", str(raw or ""))
    if not match:
        return None
    return " ".join(w.capitalize() for w in match.group(1).split()), match.group(2).upper()


def contract_terms(job: dict) -> list:
    """How a listing is contracted, from Google Jobs' schedule type, the title and the description:
    any of "contractor", "per-diem", "locums"."""
    schedule = ((job.get("detected_extensions") or {}).get("schedule_type") or "").lower()
    text = " ".join([schedule, job.get("title") or "", (job.get("description") or "")[:3000]]).lower()
    terms = []
    if "contract" in schedule or re.search(r"\b1099\b|independent contractor|\bcontract (position|role|basis|opportunity)", text):
        terms.append("contractor")
    if re.search(r"per[ -]diem|\bprn\b", text):
        terms.append("per-diem")
    if re.search(r"locum", text):
        terms.append("locums")
    if "locums" in terms and "contractor" in terms:
        terms.remove("contractor")  # locums is itself a contract role
    return terms


def pay_annual_min(pay: str):
    """Lowest yearly amount in a pay line such as "$120K–$145K a year" or "$62–$75 an hour"."""
    if not pay:
        return None
    numbers = []
    for amount, suffix in re.findall(r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*([KkMm]?)", pay):
        value = float(amount.replace(",", ""))
        value *= {"k": 1_000, "m": 1_000_000}.get(suffix.lower(), 1)
        numbers.append(value)
    if not numbers:
        return None
    low, text = min(numbers), pay.lower()
    if "hour" in text or "/hr" in text:
        low *= HOURS_PER_YEAR
    elif "month" in text:
        low *= 12
    elif "week" in text:
        low *= 52
    return int(low)


def _setting_text(candidate: dict, listing: dict) -> str:
    return " ".join(str(x or "") for x in (listing.get("title"), candidate.get("name"),
                                           candidate.get("hospital_type"), candidate.get("city"))).lower()


def listing_fits(candidate: dict, listing: dict, prefs: dict) -> bool:
    if prefs.get("min_pay") and listing.get("pay_annual_min") and listing["pay_annual_min"] < prefs["min_pay"]:
        return False
    schedule = (listing.get("schedule") or "").lower()
    if prefs.get("employment") and schedule and prefs["employment"] not in schedule:
        return False
    terms = listing.get("terms") or []
    contract = prefs.get("contract")
    if contract == "employee" and terms:
        return False
    if contract == "contractor" and not terms:
        return False
    if contract in ("per-diem", "locums") and contract not in terms:
        return False
    if prefs.get("setting") == "telehealth" and not (
            listing.get("remote") or re.search(_SETTING_WORDS["telehealth"], _setting_text(candidate, listing))):
        return False
    return True


def setting_bonus(candidate: dict, prefs: dict) -> float:
    """Fit points for the preferred setting: a bonus when the employer or its roles suit it,
    a small penalty when an inpatient psychiatric hospital meets an outpatient preference."""
    setting = prefs.get("setting")
    if not setting:
        return 0.0
    pattern = _SETTING_WORDS[setting]
    if (setting == "outpatient" and "psychiatric" in (candidate.get("hospital_type") or "").lower()
            and not any(re.search(pattern, (l.get("title") or "").lower()) for l in candidate.get("listings", []))):
        return -1.5
    if setting == "inpatient" and "psychiatric" in (candidate.get("hospital_type") or "").lower():
        return 1.5
    return 1.5 if any(re.search(pattern, _setting_text(candidate, l)) or (setting == "telehealth" and l.get("remote"))
                      for l in candidate.get("listings", [])) else 0.0
