"""Owner alerts by email through Resend, for sign-ups and requests that need a person."""

import os

import requests

OWNER_EMAIL = "info@credentialai.co"


def notify_owner(subject: str, text: str):
    """Email the owner when RESEND_API_KEY is set; otherwise the row in Supabase is the record."""
    key = os.environ.get("RESEND_API_KEY")
    if not key:
        return
    try:
        requests.post("https://api.resend.com/emails", timeout=10, headers={"Authorization": f"Bearer {key}"},
                      json={"from": "Credentialai <noreply@credentialai.co>", "to": [OWNER_EMAIL],
                            "subject": subject, "text": text})
    except requests.RequestException as exc:
        print(f"[notify] owner email failed ({exc!r})")
