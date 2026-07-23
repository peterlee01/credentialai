"""Mocked state medical/professional board license-status lookup.

There is no unified free public API for license status across all 50 state
boards (each board runs its own, often CAPTCHA-gated, non-API lookup site).
This module is an ILLUSTRATIVE DEMO DATASET ONLY, covering a representative
subset of states, so the rest of the pipeline can be exercised end-to-end.
It is not a live feed and must never be treated as verified current law.
"""

import json

from anthropic import beta_tool

DEMO_REFERENCE_NOTE = "Illustrative demo dataset — not a verified, current, or complete license feed."

# profession -> state -> status ("active" | "expired" | "pending" | "not_found")
_MOCK_LICENSE_DB = {
    "psychologist": {
        "Arizona": "active", "California": "not_found", "Colorado": "active",
        "Florida": "not_found", "Georgia": "active", "Illinois": "pending",
        "New York": "expired", "Ohio": "active", "Texas": "active", "Virginia": "pending",
    },
    "counselor": {
        "Arizona": "pending", "California": "not_found", "Colorado": "active",
        "Florida": "active", "Georgia": "expired", "Illinois": "not_found",
        "New York": "not_found", "Ohio": "active", "Texas": "pending", "Virginia": "active",
    },
    "social worker": {
        "Arizona": "active", "California": "active", "Colorado": "not_found",
        "Florida": "pending", "Georgia": "active", "Illinois": "active",
        "New York": "active", "Ohio": "not_found", "Texas": "not_found", "Virginia": "expired",
    },
}


@beta_tool
def check_license_status(profession: str, state: str) -> str:
    """Look up a clinician's professional license status in a given state.

    Args:
        profession: One of "psychologist", "counselor", "social worker".
        state: Full state name, e.g. "Colorado".
    """
    table = _MOCK_LICENSE_DB.get(profession.strip().lower(), {})
    status = table.get(state.strip(), "not_found")
    return json.dumps({
        "source": "DEMO DATA — illustrative only, not a verified license feed",
        "profession": profession,
        "state": state,
        "status": status,
        "note": DEMO_REFERENCE_NOTE,
    })
