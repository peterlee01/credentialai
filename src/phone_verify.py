"""Identity confirmation by a code sent to the practice phone on a physician's NPI record.

Uses Twilio Verify (https://www.twilio.com/docs/verify/api) over its REST API: it
generates the code, calls or texts it, and checks what the physician types back,
with its own expiry and attempt limits. The number always comes from the NPPES
registry on the server, never from the browser, so a code only reaches the
practice the registry lists for that NPI.

Needs TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_VERIFY_SERVICE_SID; without
them the feature stays off.
"""

import os
import re

import requests

VERIFY_API = "https://verify.twilio.com/v2/Services/{sid}/{path}"
CHANNELS = ("call", "sms")


class PhoneError(Exception):
    """A message the physician can act on."""


def available() -> bool:
    return all(os.environ.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_VERIFY_SERVICE_SID"))


def to_e164(phone) -> str | None:
    """A US number from the registry ("303-555-0100") as +13035550100, or None."""
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return f"+1{digits}" if len(digits) == 10 and digits[0] not in "01" else None


def mask(e164: str) -> str:
    """+13035550142 -> (303) •••-••42, enough to recognise the practice without showing the number."""
    d = e164[-10:]
    return f"({d[:3]}) •••-••{d[-2:]}"


def _post(path: str, data: dict) -> dict:
    resp = requests.post(VERIFY_API.format(sid=os.environ["TWILIO_VERIFY_SERVICE_SID"], path=path), data=data,
                         auth=(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]), timeout=15)
    if resp.status_code == 429:
        raise PhoneError("Too many codes or attempts for this number. Try again in about 10 minutes.")
    if resp.status_code == 404:
        raise PhoneError("That code has expired. Send a new one.")
    if resp.status_code >= 400:
        print(f"[phone] Twilio {path} {resp.status_code}: {resp.text[:300]}")
        raise PhoneError("We couldn't reach that number. Email info@credentialai.co and we'll confirm you another way.")
    return resp.json()


def send_code(e164: str, channel: str):
    if channel not in CHANNELS:
        raise PhoneError("Choose call or text.")
    _post("Verifications", {"To": e164, "Channel": channel})


def code_ok(e164: str, code: str) -> bool:
    code = re.sub(r"\D", "", code or "")
    if not 4 <= len(code) <= 10:
        return False
    return _post("VerificationCheck", {"To": e164, "Code": code}).get("status") == "approved"
