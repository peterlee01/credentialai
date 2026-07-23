"""Identity/practice lookup against the real, free, public NPPES NPI Registry API.

No API key required. See https://npiregistry.cms.hhs.gov/api-page
"""

import json

import requests
from anthropic import beta_tool

NPPES_ENDPOINT = "https://npiregistry.cms.hhs.gov/api/"


@beta_tool
def get_npi_identity(first_name: str, last_name: str, state: str) -> str:
    """Look up a clinician's NPI record by name and practice state via the NPPES registry.

    Args:
        first_name: Clinician's first name.
        last_name: Clinician's last name.
        state: Two-letter state code where the clinician practices.
    """
    params = {
        "version": "2.1",
        "first_name": first_name,
        "last_name": last_name,
        "state": state,
        "limit": 5,
    }
    resp = requests.get(NPPES_ENDPOINT, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    results = data.get("results", [])
    matches = []
    for r in results:
        basic = r.get("basic", {})
        taxonomies = r.get("taxonomies", [])
        primary_taxonomy = next((t for t in taxonomies if t.get("primary")), taxonomies[0] if taxonomies else {})
        matches.append({
            "npi": r.get("number"),
            "first_name": basic.get("first_name"),
            "last_name": basic.get("last_name"),
            "credential": basic.get("credential"),
            "status": basic.get("status"),  # "A" = active
            "primary_taxonomy_desc": primary_taxonomy.get("desc"),
            "primary_taxonomy_state": primary_taxonomy.get("state"),
            "license": primary_taxonomy.get("license"),
        })

    return json.dumps({
        "source": "NPPES NPI Registry (live, public)",
        "match_count": data.get("result_count", 0),
        "ambiguous": data.get("result_count", 0) > 1,
        "matches": matches,
    })
