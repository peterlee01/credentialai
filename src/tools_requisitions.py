"""Mocked hospital open-requisition matching.

Eligibility-gated matching against a hospital's own already-open roles — not a
physician-hospital marketplace. There's no physician-side discovery here and
no cross-hospital liquidity being built; a hospital that already cleared a
clinician through this pipeline can see which of its own posted roles that
clinician is now eligible for. This is an ILLUSTRATIVE DEMO DATASET ONLY,
covering a representative subset of hospitals/roles, so the pipeline can be
exercised end-to-end.
"""

import json

from anthropic import beta_tool

DEMO_REFERENCE_NOTE = "Illustrative demo dataset — not a live requisition feed from any real hospital."

# Specialty strings are drawn from real NPPES taxonomy `desc` values so they
# line up with what get_npi_identity's primary_taxonomy_desc already returns.
_MOCK_REQUISITIONS_DB = [
    {"hospital": "Mercy General", "state": "Colorado", "profession": "physician",
     "specialty": "Internal Medicine", "required_license_status": "active"},
    {"hospital": "St. Luke's Regional", "state": "Colorado", "profession": "physician",
     "specialty": "Family Medicine", "required_license_status": "active"},
    {"hospital": "Presbyterian Health", "state": "New York", "profession": "physician",
     "specialty": "Emergency Medicine", "required_license_status": "active"},
    {"hospital": "Baptist Memorial", "state": "Texas", "profession": "physician",
     "specialty": "Internal Medicine", "required_license_status": "active"},
    {"hospital": "Northwestern Community", "state": "Illinois", "profession": "physician",
     "specialty": "Anesthesiology", "required_license_status": "active"},
    {"hospital": "Sunrise Behavioral Health", "state": "Arizona", "profession": "psychologist",
     "specialty": "Clinical Psychology", "required_license_status": "active"},
    {"hospital": "Lakeside Counseling Center", "state": "Ohio", "profession": "counselor",
     "specialty": "Professional Counselor", "required_license_status": "active"},
    {"hospital": "Riverside Family Services", "state": "Georgia", "profession": "social worker",
     "specialty": "Clinical Social Worker", "required_license_status": "active"},
]


@beta_tool
def match_open_requisitions(profession: str, state: str, specialty: str = "") -> str:
    """Match a cleared clinician against a hospital's own open requisitions.

    Only call this after a clinician has already received a GO verdict — this
    checks eligibility for existing open roles, it does not perform any
    physician-side search or cross-hospital matching.

    Args:
        profession: The clinician's profession, e.g. "physician".
        state: Full state name where the clinician practices, e.g. "Colorado".
        specialty: The clinician's primary taxonomy specialty description from
            the NPPES identity result, if available, e.g. "Internal Medicine".
    """
    prof = profession.strip().lower()
    spec = specialty.strip().lower()

    matches = []
    for req in _MOCK_REQUISITIONS_DB:
        if req["profession"] != prof or req["state"] != state.strip():
            continue
        if spec and spec not in req["specialty"].lower():
            continue
        matches.append({
            "hospital": req["hospital"],
            "state": req["state"],
            "specialty": req["specialty"],
        })

    return json.dumps({
        "source": "DEMO DATA — illustrative hospital requisitions, not a live feed",
        "profession": profession,
        "state": state,
        "specialty": specialty,
        "match_count": len(matches),
        "matches": matches,
        "note": DEMO_REFERENCE_NOTE,
    })
