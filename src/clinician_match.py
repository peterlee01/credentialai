"""Matching verified clinicians to a facility's opening (the home page Facilities tab).

A facility gives its city, state, profession, setting and (optionally) a
specialty. Candidates come from two places:

- Credentialai physicians who turned on "Let employers find me" in their
  account and whose latest verification is GO ("Open to offers"), and
- the public NPPES NPI Registry: licensed clinicians practising near the
  facility, who haven't said they're looking ("Licensed nearby").

Registry candidates are checked on the spot with the same tools as a single
verification (identity from their own NPI record, the HHS-OIG exclusion list,
the state license source), and anyone with a sanction or a lapsed license is
left out. Fit is scored from the facts (location, specialty, setting, checks)
into a match %, and Claude writes the one-line reason for each match.
"""

import base64
import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import anthropic
import requests

from . import supa
from .coordinator import rule_verdict
from .report import build_report_pdf
from .summary import build_summary
from .tools_facilities import STATE_CODES
from .tools_license import check_license_status
from .tools_npi import NPPES_ENDPOINT, _PROFESSION_TAXONOMY_PREFIXES
from .tools_sanctions import check_sanctions

MODEL = "claude-opus-5-5"
RESULTS = 5
CHECKED = 7  # registry candidates checked per search; the best RESULTS are shown
SETTINGS = ("inpatient", "outpatient", "telehealth")

# Physician specialties a facility can pick, with the NPPES taxonomy text that identifies them.
PHYSICIAN_SPECIALTIES = {
    "Family Medicine": "Family Medicine",
    "Internal Medicine": "Internal Medicine",
    "Hospitalist": "Hospitalist",
    "Psychiatry": "Psychiatry",
    "Pediatrics": "Pediatrics",
    "Emergency Medicine": "Emergency Medicine",
    "Anesthesiology": "Anesthesiology",
    "Obstetrics & Gynecology": "Obstetrics & Gynecology",
    "Cardiology": "Cardiovascular Disease",
    "General Surgery": "Surgery",
}
# NPPES taxonomy text for the other professions (their whole field).
_PROFESSION_TAXONOMY = {"psychologist": "Psychologist", "counselor": "Counselor", "social worker": "Social Worker"}

# Taxonomy words that suit each setting.
_SETTING_WORDS = {
    "inpatient": r"hospitalist|emergency|critical care|anesthes|surgery|psychiatr|neonatal|clinical",
    "outpatient": r"family|internal|pediatric|counsel|clinical|mental health|school|obstetric",
    "telehealth": r"psychiatr|counsel|clinical|mental health|internal|family",
}


# ---------- registry candidates ----------

def _nppes(params: dict) -> list:
    try:
        resp = requests.get(NPPES_ENDPOINT, params={"version": "2.1", "enumeration_type": "NPI-1", "limit": 200,
                                                    **params}, timeout=15)
        resp.raise_for_status()
        return resp.json().get("results") or []
    except (requests.RequestException, ValueError) as exc:
        print(f"[clinicians] NPPES search failed: {exc!r}")
        return []


def _from_record(record: dict, profession: str, state_code: str, wanted: str):
    """A candidate from one NPPES record, or None when it isn't an active clinician of the profession."""
    basic = record.get("basic") or {}
    if basic.get("status", "A") != "A":
        return None
    prefixes = _PROFESSION_TAXONOMY_PREFIXES.get(profession, ())
    fitting = [t for t in record.get("taxonomies") or [] if (t.get("code") or "").startswith(prefixes)]
    if wanted:
        fitting = [t for t in fitting if wanted.lower() in (t.get("desc") or "").lower()]
    if not fitting:
        return None
    primary = next((t for t in fitting if t.get("primary")), fitting[0])
    in_state = next((t for t in fitting if (t.get("state") or "").upper() == state_code and t.get("license")), None)
    addresses = record.get("addresses") or []
    practice = next((a for a in addresses if a.get("address_purpose") == "LOCATION"), addresses[0] if addresses else {})
    if (practice.get("state") or "").upper() != state_code:
        return None
    return {
        "source": "registry",
        "npi": str(record.get("number")),
        "first_name": (basic.get("first_name") or "").title(),
        "last_name": (basic.get("last_name") or "").title(),
        "credential": basic.get("credential") or "",
        "specialty": primary.get("desc") or profession.title(),
        "license": (in_state or {}).get("license") or "",
        "city": (practice.get("city") or "").title(),
        "zip": (practice.get("postal_code") or "")[:5],
        "updated": basic.get("last_updated") or basic.get("enumeration_date") or "",
    }


def registry_candidates(city: str, state: str, profession: str, specialty: str = "") -> list:
    """Active clinicians of the profession practising in the city (or, if too few, the state)."""
    state_code = STATE_CODES.get(state, state)
    wanted = PHYSICIAN_SPECIALTIES.get(specialty, "") if profession == "physician" else ""
    taxonomy = wanted or _PROFESSION_TAXONOMY.get(profession, "")
    found = {}

    def add(records):
        for record in records:
            candidate = _from_record(record, profession, state_code, wanted)
            if candidate:
                found.setdefault(candidate["npi"], candidate)

    base = {"state": state_code, "address_purpose": "LOCATION"}
    if taxonomy:
        add(_nppes({**base, "city": city, "taxonomy_description": taxonomy}))
    if len(found) < CHECKED:
        # The registry's taxonomy search doesn't catch every wording, so also page through the
        # city's records and keep the ones in the profession.
        for skip in (0, 200, 400):
            batch = _nppes({**base, "city": city, "skip": skip})
            add(batch)
            if len(batch) < 200 or len(found) >= CHECKED * 2:
                break
    if len(found) < CHECKED and taxonomy:
        add(_nppes({**base, "taxonomy_description": taxonomy}))  # the rest of the state
    return list(found.values())


# ---------- opt-in physicians ----------

def opt_in_candidates(state: str, profession: str) -> list:
    """Credentialai physicians in the state who let employers find them and are verified (GO)."""
    try:
        profiles = supa.select("profiles", {"state": f"eq.{state}", "profession": f"eq.{profession}",
                                            "prefs->>discoverable": "eq.true", "select": "*"})
    except supa.NotConfigured:
        return []
    except Exception as exc:
        print(f"[clinicians] opt-in lookup failed: {exc!r}")
        return []
    out = []
    for p in profiles or []:
        latest = supa.select("verifications", {"user_id": f"eq.{p['id']}", "order": "created_at.desc",
                                               "limit": "1", "select": "*"})
        if not latest or (latest[0].get("verdict") or "").upper() != "GO":
            continue
        out.append({
            "source": "opt_in",
            "npi": p.get("npi"),
            "first_name": p.get("first_name"),
            "last_name": p.get("last_name"),
            "credential": "",
            "specialty": p.get("specialty") or profession.title(),
            "license": "",
            "city": (p.get("practice_city") or "").title(),
            "zip": "",
            "updated": "",
            "prefs": p.get("prefs") or {},
            "verdict_text": latest[0].get("verdict_text"),
            "summary": latest[0].get("summary"),
            "checked_at": latest[0].get("created_at"),
        })
    return out


# ---------- checks ----------

def _check(candidate: dict, state: str, profession: str) -> dict:
    """Run the sanctions and license checks on a registry candidate; identity is their own NPI record."""
    first, last = candidate["first_name"], candidate["last_name"]
    identity = {"source": "NPPES NPI Registry (live, public)", "match_count": 1, "name_matches_other_professions": 0,
                "matches": [{"npi": candidate["npi"], "first_name": first, "last_name": last,
                             "credential": candidate["credential"], "primary_taxonomy_desc": candidate["specialty"],
                             "license": candidate["license"], "practice_city": candidate["city"],
                             "practice_zip": candidate["zip"]}]}
    try:
        sanctions = json.loads(check_sanctions.call({"first_name": first, "last_name": last}))
        # A same-name entry with a different NPI is a different person.
        hits = [h for h in sanctions.get("hits", [])
                if (h.get("npi") or "").strip() in ("", "0000000000", candidate["npi"])]
        sanctions = {**sanctions, "hits": hits, "hit_count": len(hits)}
    except Exception as exc:
        print(f"[clinicians] sanctions check failed: {exc!r}")
        sanctions = None
    try:
        license_ = json.loads(check_license_status.call({"first_name": first, "last_name": last, "profession": profession,
                                                         "state": state, "license_number": candidate["license"]}))
    except Exception as exc:
        print(f"[clinicians] license check failed: {exc!r}")
        license_ = None
    tool_calls = [{"name": "get_npi_identity", "result": identity}]
    if sanctions is not None:
        tool_calls.append({"name": "check_sanctions", "result": sanctions})
    if license_ is not None:
        tool_calls.append({"name": "check_license_status", "result": license_})
    summary = build_summary(first, last, state, profession, tool_calls)
    verdict = rule_verdict(tool_calls) if sanctions is not None and license_ is not None else "NEEDS REVIEW"
    return {**candidate, "summary": summary, "verdict": verdict,
            "license_status": (license_ or {}).get("status"), "sanctions_hits": (sanctions or {}).get("hit_count")}


def _verdict_text(c: dict, state: str) -> str:
    name = f"{c['first_name']} {c['last_name']}"
    checks = {row["label"]: row for row in (c.get("summary") or {}).get("checks", [])}
    license_row = checks.get("License", {})
    if c["verdict"] == "GO":
        text = (f"{name} is listed in the national provider registry as {c['specialty']} in {c['city'] or state}, "
                f"doesn't appear on the federal list of excluded providers, and the license is active.")
    elif c["verdict"] == "NO-GO":
        text = f"{name} has a sanction or license problem on record; see the checks below."
    else:
        text = (f"{name} is listed in the national provider registry as {c['specialty']} and doesn't appear on the "
                f"federal list of excluded providers. {license_row.get('detail', 'The license needs a person to confirm it.')}")
    return f"{c['verdict']}\n{text}"


# ---------- scoring ----------

def _score(c: dict, city: str, setting: str, specialty: str) -> tuple:
    """(points, best possible, signals) for one checked candidate."""
    points, signals = 0.0, []
    best = 4 + 2 + 1 + 3
    if c["city"].lower() == city.lower():
        points += 4
        signals.append(f"Practices in {c['city']}")
    else:
        points += 2
        signals.append(f"Practices in {c['city'] or 'the state'}")
    desc = (c.get("specialty") or "").lower()
    if specialty:
        best += 3
        if (PHYSICIAN_SPECIALTIES.get(specialty, specialty)).lower() in desc:
            points += 3
            signals.append(f"{specialty} specialty")
    if setting and re.search(_SETTING_WORDS[setting], desc):
        points += 2
        signals.append(f"Specialty suits {setting} work")
    if c["source"] == "opt_in":
        points += 1
        if setting and (c.get("prefs") or {}).get("setting") == setting:
            points += 1
            signals.append(f"Wants {setting} work")
    elif c.get("updated", "")[:4].isdigit() and int(c["updated"][:4]) >= date.today().year - 2:
        points += 1
        signals.append(f"Registry record updated {c['updated'][:4]}")
    if c["verdict"] == "GO":
        points += 3
        signals.append("Identity, sanctions and license all clear")
    elif c.get("license_status") in ("unverified", "not_found", "ambiguous"):
        signals.append("License needs a person to confirm")
    if c["source"] == "opt_in":
        best += 1
    return points, best, signals


# ---------- AI reasons ----------

_REASONS_SCHEMA = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"npi": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["npi", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["matches"],
    "additionalProperties": False,
}


def _ai_reasons(facility: dict, candidates: list) -> dict:
    """{npi: one-sentence reason} written by Claude from the facts, or {} if the call fails."""
    facts = [{"npi": c["npi"], "name": f"{c['first_name']} {c['last_name']}", "specialty": c["specialty"],
              "practice_city": c["city"], "open_to_offers": c["source"] == "opt_in", "match_pct": c["match_pct"],
              "verdict": c["verdict"], "signals": c["signals"]} for c in candidates]
    prompt = (
        "A healthcare facility is looking for a clinician. Facility: " + json.dumps(facility) + "\n"
        "Verified candidates, with the facts behind each match: " + json.dumps(facts) + "\n\n"
        "For each candidate, write one plain sentence (under 20 words) for the facility's hiring manager "
        "explaining why they fit or what to check, using only the facts given. Say 'open to offers' only "
        "when open_to_offers is true. Refer to each clinician by last name, never as he or she."
    )
    try:
        response = anthropic.Anthropic().messages.create(
            model=MODEL,
            max_tokens=4000,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": _REASONS_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            return {}
        text = next(b.text for b in response.content if b.type == "text")
        return {str(m["npi"]): m["reason"].strip() for m in json.loads(text)["matches"] if m.get("reason")}
    except (anthropic.APIError, StopIteration, ValueError, KeyError) as exc:
        print(f"[clinicians] reasons failed: {exc!r}")
        return {}


# ---------- the match ----------

def match_clinicians(city: str, state: str, profession: str, setting: str = "", specialty: str = "",
                     with_reports: bool = True) -> dict:
    """Up to RESULTS verified clinicians for a facility's opening, best fit first."""
    registry = registry_candidates(city, state, profession, specialty)
    opt_in = opt_in_candidates(state, profession)
    opt_in_npis = {c["npi"] for c in opt_in}
    registry = [c for c in registry if c["npi"] not in opt_in_npis]

    # Check the closest, most recently updated registry records first.
    registry.sort(key=lambda c: c["updated"][:10], reverse=True)
    registry.sort(key=lambda c: (c["city"].lower() != city.lower(), not c["license"]))
    with ThreadPoolExecutor(max_workers=4) as pool:
        checked = list(pool.map(lambda c: _check(c, state, profession), registry[:CHECKED]))
    excluded = sum(1 for c in checked if c["verdict"] == "NO-GO")
    checked = [c for c in checked if c["verdict"] != "NO-GO"]
    for c in opt_in:
        c["verdict"] = "GO"

    pool_ = []
    for c in opt_in + checked:
        points, best, signals = _score(c, city, setting, specialty)
        pool_.append({**c, "match_pct": min(100, round(100 * points / best)), "signals": signals})
    # Open-to-offers physicians first, then by fit.
    pool_.sort(key=lambda c: (c["source"] != "opt_in", -c["match_pct"], c["last_name"]))
    shown = pool_[:RESULTS]

    facility = {"city": city, "state": state, "profession": profession, "setting": setting or "any",
                "specialty": specialty or "any"}
    reasons = _ai_reasons(facility, shown) if shown else {}

    matches = []
    for c in shown:
        report = None
        if with_reports:
            try:
                report_id = uuid.uuid4().hex[:12].upper()
                verdict_text = c.get("verdict_text") or _verdict_text(c, state)
                pdf = build_report_pdf(c["first_name"], c["last_name"], state, profession, verdict_text,
                                       c.get("summary"), report_id=report_id)
                report = {"id": report_id, "filename": f"credentialai-report-{c['last_name']}-{report_id}.pdf".lower(),
                          "pdf_base64": base64.b64encode(pdf).decode("ascii")}
            except Exception as exc:
                print(f"[clinicians] report failed ({exc.__class__.__name__})")
        matches.append({
            "npi": c["npi"],
            "name": " ".join(p for p in (c["first_name"], c["last_name"]) if p),
            "credential": c.get("credential"),
            "specialty": c["specialty"],
            "city": c["city"],
            "open_to_offers": c["source"] == "opt_in",
            "match_pct": c["match_pct"],
            "reason": reasons.get(str(c["npi"])) or "; ".join(c["signals"]) + ".",
            "ranked_by": "ai" if str(c["npi"]) in reasons else "score",
            "verdict": c["verdict"],
            "checks": (c.get("summary") or {}).get("checks", []),
            "report": report,
        })
    print(f"[clinicians] {profession} in {city}, {state}: {len(registry)} registry, {len(opt_in)} opt-in, "
          f"{excluded} left out for a sanction or license problem, {len(matches)} shown")
    return {"matches": matches, "registry_found": len(registry), "opt_in_found": len(opt_in), "excluded": excluded}
