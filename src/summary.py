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


NPPES_SEARCH = "https://npiregistry.cms.hhs.gov/"
NPPES_UPDATE = "https://nppes.cms.hhs.gov/"
OIG_SEARCH = "https://exclusions.oig.hhs.gov/"
OIG_REINSTATEMENT = "https://oig.hhs.gov/exclusions/"
STATE_BOARDS = "https://www.fsmb.org/contact-a-state-medical-board/"


def _step(text: str, label: str = "", url: str = "") -> dict:
    return {"text": text, "link": {"label": label, "url": url} if url else None}


def next_steps(state: str, profession: str, identity, sanctions, license_) -> dict:
    """What a facility and a physician should do about a NEEDS REVIEW or NO-GO result,
    one step per check that didn't clear. Empty lists when every check cleared."""
    facility, physician = [], []
    count = (identity or {}).get("match_count", 0)
    if identity is not None and count > 1:
        facility.append(_step("Several registry records share this name. Ask the physician for their NPI number "
                              "to confirm which record is theirs.", "Search the NPI Registry", NPPES_SEARCH))
        physician.append(_step("Several registry records share your name. Check that your name and practice "
                               "state match your NPI record exactly.", "Update your NPI record", NPPES_UPDATE))
    elif identity is not None and count == 0:
        facility.append(_step("No registry record matched. Check the spelling, state and profession, or ask the "
                              "physician for their NPI number.", "Search the NPI Registry", NPPES_SEARCH))
        physician.append(_step("We couldn't find your registry record. Make sure your name, practice state and "
                               "profession match your NPI record, and update it if it's out of date.",
                               "Update your NPI record", NPPES_UPDATE))

    if sanctions is not None and sanctions.get("hit_count", 0) > 0:
        facility.append(_step("A matching name is on the federal exclusion list. Confirm it's the same person, using "
                              "their date of birth or NPI, before you decide. Federal health programs won't pay for "
                              "services from an excluded provider.", "Search the OIG exclusion list", OIG_SEARCH))
        physician.append(_step("A matching name is on the HHS-OIG exclusion list. If it isn't you, or your exclusion "
                               "has ended, contact HHS-OIG to correct the record or apply for reinstatement.",
                               "HHS-OIG exclusions", OIG_REINSTATEMENT))

    status = (license_ or {}).get("status")
    record = ((license_ or {}).get("records") or [{}])[0] or {}
    board = record.get("board_url") or (STATE_BOARDS if profession == "physician" else "")
    board_label = f"{state} licensing board" if record.get("board_url") else "Find the state medical board"
    if status in ("expired", "revoked"):
        facility.append(_step(f"The license is {status}. The physician shouldn't practice until it's active again. "
                              "Check the board's record for the reason and any renewal.", board_label, board))
        physician.append(_step(f"Your {state} license shows as {status}. Resolve it with the state board, then run "
                               "the check again.", board_label, board))
    elif status == "unverified":
        facility.append(_step(f"{state} licenses can't be checked automatically yet. Look the license up on the state "
                              "board's website, or ask the physician for a copy.", board_label, board))
        physician.append(_step(f"We can't check {state} licenses automatically yet, so a facility will confirm your "
                               "license with the state board. Keep your license number handy.", board_label, board))
    elif status == "pending":
        facility.append(_step("The license is pending. Confirm it's active with the state board before the "
                              "physician starts.", board_label, board))
        physician.append(_step("Your license shows as pending. Run the check again once the board marks it active.",
                               board_label, board))
    elif status == "ambiguous":
        facility.append(_step("Several license records share this name. Ask the physician for their license number "
                              "and confirm it with the state board.", board_label, board))
        physician.append(_step("Several license records share your name. Make sure the name on your license matches "
                               "your NPI record.", board_label, board))
    elif license_ is not None and status != "active":
        facility.append(_step("No license record matched this name. Ask the physician for their license number and "
                              "confirm it with the state board.", board_label, board))
        physician.append(_step("No license record matched your name. Make sure the name on your license matches your "
                               "NPI record, and that the right state is selected.", board_label, board))

    if facility:
        facility.append(_step("Download the PDF report and keep it in the physician's credentialing file. "
                              "Your credentialing team makes the final decision."))
        physician.append(_step("Once it's resolved, run the check again to update your verified profile."))
    return {"facility": facility, "physician": physician}


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
        license_source = license_.get("source_label") or ("Colorado DORA" if license_.get("source") else "State board")
        record = (license_.get("records") or [None])[0]
        if record and license_.get("match_count") == 1:
            number, kind = record.get("license_number"), record.get("license_type")
            parts = [("License " + " ".join(p for p in (str(number or ""), f"({kind})" if kind else "") if p)).strip(),
                     f"status {record.get('status_text') or 'unknown'}"]
            if record.get("expiration_date"):
                parts.append(f"expires {_long_date(record['expiration_date'])}")
            elif record.get("data_as_of"):
                parts.append("the board's roster doesn't list an expiry date")
            if record.get("data_as_of"):
                parts.append(f"board roster as of {_long_date(record['data_as_of'])}")
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
         "source_url": (license_ or {}).get("source_url") or (
             "https://data.colorado.gov/resource/7s5z-vewr" if license_source == "Colorado DORA" else None)},
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
        "next_steps": next_steps(state, profession, identity, sanctions, license_),
    }
