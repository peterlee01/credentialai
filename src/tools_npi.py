"""Identity/practice lookup against the real, free, public NPPES NPI Registry API.

No API key required. See https://npiregistry.cms.hhs.gov/api-page
"""

import json

import requests
from anthropic import beta_tool

NPPES_ENDPOINT = "https://npiregistry.cms.hhs.gov/api/"

# NPPES taxonomy code prefixes for each supported profession, so a name search
# only counts records in the right field (a psychologist search ignores a
# physical therapy assistant with the same name).
_PROFESSION_TAXONOMY_PREFIXES = {
    "psychologist": ("103T",),
    "counselor": ("101Y",),
    "social worker": ("1041",),
    "physician": ("20",),  # Allopathic & Osteopathic Physicians
}


@beta_tool
def get_npi_identity(first_name: str, last_name: str, state: str, profession: str, npi: str = "") -> str:
    """Look up a clinician's NPI record by name, practice state and profession via the NPPES registry.

    Args:
        first_name: Clinician's first name.
        last_name: Clinician's last name.
        state: Two-letter state code where the clinician practices.
        profession: One of "psychologist", "counselor", "social worker", "physician".
        npi: The clinician's NPI number, when the request gives one. Looks up that exact record.
    """
    prefixes = _PROFESSION_TAXONOMY_PREFIXES.get(profession.strip().lower(), ())
    npi = "".join(ch for ch in (npi or "") if ch.isdigit())
    if npi:
        params = {"version": "2.1", "number": npi}
    else:
        params = {
            "version": "2.1",
            "first_name": first_name,
            "last_name": last_name,
            "state": state,
            "limit": 20,
        }
    resp = requests.get(NPPES_ENDPOINT, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    results = data.get("results", [])
    matches = []
    for r in results:
        basic = r.get("basic", {})
        taxonomies = r.get("taxonomies", [])
        fitting = [t for t in taxonomies if not prefixes or (t.get("code") or "").startswith(prefixes)]
        if not fitting:
            continue
        primary_taxonomy = next((t for t in fitting if t.get("primary")), fitting[0])
        addresses = r.get("addresses", [])
        practice = next((a for a in addresses if a.get("address_purpose") == "LOCATION"), addresses[0] if addresses else {})
        matches.append({
            "npi": r.get("number"),
            "first_name": basic.get("first_name"),
            "last_name": basic.get("last_name"),
            "credential": basic.get("credential"),
            "status": basic.get("status"),  # "A" = active
            "primary_taxonomy_desc": primary_taxonomy.get("desc"),
            "primary_taxonomy_state": primary_taxonomy.get("state"),
            "license": primary_taxonomy.get("license"),
            "practice_city": practice.get("city"),
            "practice_zip": (practice.get("postal_code") or "")[:5],
        })

    return json.dumps({
        "source": "NPPES NPI Registry (live, public)",
        "profession": profession,
        "match_count": len(matches),
        "ambiguous": len(matches) > 1,
        "name_matches_other_professions": len(results) - len(matches),
        "matches": matches,
    })
