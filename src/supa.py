"""Minimal Supabase access for the server: who is signed in, and their rows.

Uses Supabase's REST endpoints directly with requests (no extra dependency):
  - GET /auth/v1/user with the physician's access token tells us who they are;
  - /rest/v1/<table> with the service role key reads and writes rows;
  - /auth/v1/admin/users/<id> deletes an account.
The secret key (new sb_secret_... or legacy service_role) bypasses row-level
security, so it stays on the server (Vercel env SUPABASE_SERVICE_ROLE_KEY) and
every query here is scoped to the signed-in user's id. The browser uses the
publishable key (sb_publishable_... or legacy anon) as SUPABASE_ANON_KEY.
"""

import os

import requests

TIMEOUT = 15


class NotConfigured(Exception):
    pass


def _env():
    url = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    anon = os.environ.get("SUPABASE_ANON_KEY") or ""
    service = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""
    if not (url and anon and service):
        raise NotConfigured()
    return url, anon, service


def public_config():
    """What the browser needs to sign in: project URL and the public (anon) key."""
    url = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    anon = os.environ.get("SUPABASE_ANON_KEY") or ""
    return {"supabase_url": url, "supabase_anon_key": anon} if url and anon else None


def user_from_token(token: str):
    """The signed-in user ({"id", "email"}) for an access token, or None."""
    url, anon, _ = _env()
    if not token:
        return None
    resp = requests.get(f"{url}/auth/v1/user", headers={"apikey": anon, "Authorization": f"Bearer {token}"},
                        timeout=TIMEOUT)
    if resp.status_code != 200:
        return None
    data = resp.json()
    return {"id": data.get("id"), "email": data.get("email")} if data.get("id") else None


def _headers(extra=None):
    _, _, service = _env()
    headers = {"apikey": service, "Content-Type": "application/json"}
    # New-style secret keys (sb_secret_...) go only in the apikey header; Authorization: Bearer is for
    # JWTs. Legacy service_role keys are JWTs and are also sent as the bearer token.
    if not service.startswith("sb_"):
        headers["Authorization"] = f"Bearer {service}"
    return {**headers, **(extra or {})}


def select(table: str, params: dict):
    url, _, _ = _env()
    resp = requests.get(f"{url}/rest/v1/{table}", params=params, headers=_headers(), timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def upsert(table: str, row: dict):
    url, _, _ = _env()
    resp = requests.post(f"{url}/rest/v1/{table}", json=row, timeout=TIMEOUT,
                         headers=_headers({"Prefer": "resolution=merge-duplicates,return=representation"}))
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else None


def insert(table: str, row: dict):
    url, _, _ = _env()
    resp = requests.post(f"{url}/rest/v1/{table}", json=row, timeout=TIMEOUT,
                         headers=_headers({"Prefer": "return=representation"}))
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else None


def update(table: str, match: dict, changes: dict):
    url, _, _ = _env()
    params = {k: f"eq.{v}" for k, v in match.items()}
    resp = requests.patch(f"{url}/rest/v1/{table}", params=params, json=changes, timeout=TIMEOUT,
                          headers=_headers({"Prefer": "return=representation"}))
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else None


def delete_user(user_id: str):
    """Delete the auth user; their profile and verifications go with it (on delete cascade)."""
    url, _, _ = _env()
    resp = requests.delete(f"{url}/auth/v1/admin/users/{user_id}", headers=_headers(), timeout=TIMEOUT)
    resp.raise_for_status()


def delete_rows(table: str, match: dict):
    """Delete the rows matching every column in `match`."""
    url, _, _ = _env()
    params = {k: f"eq.{v}" for k, v in match.items()}
    resp = requests.delete(f"{url}/rest/v1/{table}", params=params, headers=_headers(), timeout=TIMEOUT)
    resp.raise_for_status()
