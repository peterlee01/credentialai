"""The live demo's per-visitor limit, used by /api/verify.

A cookie counts how many runs a browser has used; a best-effort, in-memory
per-warm-instance IP count backs it up (the privacy policy describes this:
"your IP address, used briefly and only in server memory"). The owner pass, a
request carrying the DEMO_BYPASS_KEY secret in X-Demo-Key, skips the limit and
isn't counted.
"""

import hmac
import http.cookies
import os
import time

RUNS_PER_VISITOR = 3
COOKIE_NAME = "cv_used"  # holds how many runs this browser has used
COOKIE_MAX_AGE = 60 * 60 * 24 * 180  # 180 days

_seen_ips = {}  # IP -> runs used
_seen_ips_reset_at = time.time()
_SEEN_IPS_TTL = 60 * 60  # clear the in-memory set hourly to bound its size


def check(headers, client_address) -> dict:
    """{"owner", "runs_used", "client_ip", "allowed"} for this request."""
    global _seen_ips_reset_at
    if time.time() - _seen_ips_reset_at > _SEEN_IPS_TTL:
        _seen_ips.clear()
        _seen_ips_reset_at = time.time()

    bypass_key = os.environ.get("DEMO_BYPASS_KEY", "")
    owner = bool(bypass_key) and hmac.compare_digest(headers.get("X-Demo-Key", "").encode(), bypass_key.encode())

    cookies = http.cookies.SimpleCookie(headers.get("Cookie", ""))
    client_ip = headers.get("x-forwarded-for", client_address[0])
    try:
        cookie_runs = int(cookies[COOKIE_NAME].value) if COOKIE_NAME in cookies else 0
    except ValueError:
        cookie_runs = RUNS_PER_VISITOR  # an unreadable count is treated as used up
    runs_used = max(cookie_runs, _seen_ips.get(client_ip, 0))
    return {"owner": owner, "runs_used": runs_used, "client_ip": client_ip,
            "allowed": owner or runs_used < RUNS_PER_VISITOR}


def record_run(visitor: dict) -> int:
    """Count one run for a non-owner visitor; returns the new total."""
    runs_used = visitor["runs_used"] + 1
    _seen_ips[visitor["client_ip"]] = runs_used
    return runs_used


def cookie_header(runs_used: int) -> str:
    return f"{COOKIE_NAME}={runs_used}; Max-Age={COOKIE_MAX_AGE}; Path=/; HttpOnly; Secure; SameSite=Lax"
