"""Plain summary of a verification for the product UI: who was checked, and
what each of the three checks found, read straight from the tool results."""

_LICENSE_LABELS = {
    "active": ("Active", "ok"),
    "expired": ("Expired", "bad"),
    "revoked": ("Revoked", "bad"),
    "pending": ("Pending", "warn"),
    "not_found": ("Not found", "warn"),
    "ambiguous": ("Needs review", "warn"),
    "unverified": ("Needs review", "warn"),
}


def _result(tool_calls: list, name: str):
    call = next((t for t in reversed(tool_calls) if t["name"] == name), None)
    return None if call is None else (call.get("result") or {})


def build_summary(first_name: str, last_name: str, state: str, profession: str, tool_calls: list) -> dict:
    identity = _result(tool_calls, "get_npi_identity")
    sanctions = _result(tool_calls, "check_sanctions")
    license_ = _result(tool_calls, "check_license_status")

    specialty = profession.title()
    if identity is None:
        id_status = ("Not run", "")
    else:
        count = identity.get("match_count", 0)
        if count == 1:
            id_status = ("Match found", "ok")
            specialty = identity["matches"][0].get("primary_taxonomy_desc") or specialty
        elif count > 1:
            id_status = (f"{count} possible matches", "warn")
        else:
            id_status = ("No match", "warn")

    if sanctions is None:
        sanction_status = ("Not run", "")
    else:
        hits = sanctions.get("hit_count", 0)
        sanction_status = ("No hits", "ok") if hits == 0 else (f"{hits} {'hit' if hits == 1 else 'hits'}", "bad")

    if license_ is None:
        license_status, license_source = ("Not run", ""), "State board"
    else:
        license_status = _LICENSE_LABELS.get(license_.get("status"), ("Needs review", "warn"))
        license_source = "Colorado DORA" if license_.get("source") else "State board"

    rows = [
        {"label": "Identity", "source": "NPPES NPI Registry", "status": id_status[0], "tone": id_status[1]},
        {"label": "Sanctions", "source": "HHS-OIG LEIE", "status": sanction_status[0], "tone": sanction_status[1]},
        {"label": "License", "source": license_source, "status": license_status[0], "tone": license_status[1]},
    ]
    return {
        "person": {
            "name": f"{first_name} {last_name}".strip(),
            "initials": (first_name[:1] + last_name[:1]).upper(),
            "subtitle": f"{specialty} · {state}",
        },
        "checks": rows,
        "checks_clear": sum(1 for r in rows if r["tone"] == "ok"),
    }
