"""Plain summary of a verification for the product UI: who was checked, and
what each of the three checks found, read straight from the tool results."""

from datetime import datetime

_LICENSE_LABELS = {
    "active": ("Active", "ok"),
    "expired": ("Expired", "bad"),
    "revoked": ("Revoked", "bad"),
    "pending": ("Pending", "warn"),
    "not_found": ("Not found", "warn"),
    "ambiguous": ("Needs review", "warn"),
    "unverified": ("Needs review", "warn"),
}


def _long_date(iso: str) -> str:
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%B %-d, %Y")
    except ValueError:
        return iso


def _result(tool_calls: list, name: str):
    call = next((t for t in reversed(tool_calls) if t["name"] == name), None)
    return None if call is None else (call.get("result") or {})


def build_summary(first_name: str, last_name: str, state: str, profession: str, tool_calls: list) -> dict:
    identity = _result(tool_calls, "get_npi_identity")
    sanctions = _result(tool_calls, "check_sanctions")
    license_ = _result(tool_calls, "check_license_status")

    specialty, npi = profession.title(), None
    if identity is None:
        id_status, id_detail = ("Not run", ""), "The identity check did not run."
    else:
        count = identity.get("match_count", 0)
        if count == 1:
            id_status = ("Match found", "ok")
            match = identity["matches"][0]
            specialty = match.get("primary_taxonomy_desc") or specialty
            npi = match.get("npi")
            place = ", ".join(p for p in (str(match.get("practice_city") or "").title(), state) if p)
            id_detail = f"NPI {npi}, {specialty}, practicing in {place}."
            if match.get("license"):
                id_detail += f" Registry license number {match['license']}."
        elif count > 1:
            id_status = (f"{count} possible matches", "warn")
            id_detail = f"{count} registry records in this profession share the name; a person must confirm which one."
        else:
            id_status = ("No match", "warn")
            id_detail = "No registry record in this profession matched the name and state."
        others = identity.get("name_matches_other_professions") or 0
        if others:
            id_detail += f" {others} same-name record(s) in other professions were set aside."

    if sanctions is None:
        sanction_status, sanction_detail = ("Not run", ""), "The sanctions check did not run."
    else:
        hits = sanctions.get("hit_count", 0)
        sanction_status = ("No hits", "ok") if hits == 0 else (f"{hits} {'hit' if hits == 1 else 'hits'}", "bad")
        if hits == 0:
            sanction_detail = "No matching entry on the HHS-OIG List of Excluded Individuals/Entities."
        else:
            first = (sanctions.get("hits") or [{}])[0]
            sanction_detail = (f"{hits} matching exclusion entr{'y' if hits == 1 else 'ies'}; first: type "
                               f"{first.get('excl_type') or 'n/a'}, dated {first.get('excl_date') or 'n/a'}, "
                               f"state {first.get('state') or 'n/a'}.")

    if license_ is None:
        license_status, license_source = ("Not run", ""), "State board"
        license_detail = "The license check did not run."
    else:
        license_status = _LICENSE_LABELS.get(license_.get("status"), ("Needs review", "warn"))
        license_source = "Colorado DORA" if license_.get("source") else "State board"
        record = (license_.get("records") or [None])[0]
        if record and license_.get("match_count") == 1:
            parts = [f"License {record.get('license_number')} ({record.get('license_type')})",
                     f"status {record.get('status_text') or 'unknown'}"]
            if record.get("expiration_date"):
                parts.append(f"expires {_long_date(record['expiration_date'])}")
            parts.append("discipline on record" if record.get("discipline_on_record") else "no discipline on record")
            license_detail = ", ".join(parts) + "."
        elif license_.get("note"):
            license_detail = license_["note"]
        elif license_.get("status") == "ambiguous":
            license_detail = f"{license_.get('match_count')} license records share the name; a person must confirm."
        else:
            license_detail = "No license record found for this name and profession."

    rows = [
        {"label": "Identity", "source": "NPPES NPI Registry", "status": id_status[0], "tone": id_status[1],
         "detail": id_detail, "source_url": "https://npiregistry.cms.hhs.gov"},
        {"label": "Sanctions", "source": "HHS-OIG LEIE", "status": sanction_status[0], "tone": sanction_status[1],
         "detail": sanction_detail, "source_url": "https://oig.hhs.gov/exclusions"},
        {"label": "License", "source": license_source, "status": license_status[0], "tone": license_status[1],
         "detail": license_detail,
         "source_url": "https://data.colorado.gov/resource/7s5z-vewr" if license_source == "Colorado DORA" else None},
    ]
    return {
        "person": {
            "name": f"{first_name} {last_name}".strip(),
            "initials": (first_name[:1] + last_name[:1]).upper(),
            "subtitle": f"{specialty} · {state}",
            "specialty": specialty,
            "npi": npi,
        },
        "checks": rows,
        "checks_clear": sum(1 for r in rows if r["tone"] == "ok"),
    }
