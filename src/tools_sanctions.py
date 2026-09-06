"""Sanctions/exclusion check against the real, free, public HHS-OIG LEIE database.

Downloads and locally caches the official monthly CSV from
https://oig.hhs.gov/exclusions/exclusions_list.asp — no API key required.
"""

import csv
import json
import os
import tempfile
import time

import requests
from anthropic import beta_tool

LEIE_URL = "https://oig.hhs.gov/exclusions/downloadables/UPDATED.csv"
CACHE_PATH = os.path.join(tempfile.gettempdir(), "UPDATED.csv")
CACHE_MAX_AGE_SECONDS = 24 * 60 * 60  # re-download at most once a day

_leie_rows_cache = None


def _ensure_local_copy() -> str:
    needs_download = (
        not os.path.exists(CACHE_PATH)
        or (time.time() - os.path.getmtime(CACHE_PATH)) > CACHE_MAX_AGE_SECONDS
    )
    if needs_download:
        resp = requests.get(LEIE_URL, timeout=60)
        resp.raise_for_status()
        with open(CACHE_PATH, "wb") as f:
            f.write(resp.content)
    return CACHE_PATH


def _load_rows() -> list:
    global _leie_rows_cache
    if _leie_rows_cache is not None:
        return _leie_rows_cache

    path = _ensure_local_copy()
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        _leie_rows_cache = list(reader)
    return _leie_rows_cache


@beta_tool
def check_sanctions(first_name: str, last_name: str) -> str:
    """Check whether a clinician appears on the HHS-OIG List of Excluded Individuals/Entities.

    Args:
        first_name: Clinician's first name.
        last_name: Clinician's last name.
    """
    rows = _load_rows()
    fn, ln = first_name.strip().upper(), last_name.strip().upper()

    hits = []
    for row in rows:
        if row.get("LASTNAME", "").strip().upper() == ln and row.get("FIRSTNAME", "").strip().upper() == fn:
            hits.append({
                "npi": row.get("NPI"),
                "excl_type": row.get("EXCLTYPE"),
                "excl_date": row.get("EXCLDATE"),
                "state": row.get("STATE"),
                "specialty": row.get("SPECIALTY"),
            })

    return json.dumps({
        "source": "HHS-OIG LEIE (live, public, monthly CSV)",
        "hit_count": len(hits),
        "hits": hits,
    })
