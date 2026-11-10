"""Redaction for provider payloads before they are logged or saved as evidence.

Emails, names, phone numbers, addresses, tokens and hosted payer URLs are replaced by a
short SHA-256 fingerprint so evidence can still be correlated without exposing them.
"""

import hashlib
import re
from typing import Any

_SENSITIVE_KEYS = {
    "access_token",
    "refresh_token",
    "id_token",
    "nonce",
    "client_secret",
    "authorization",
    "email_address",
    "email",
    "phones",
    "phone",
    "national_number",
    "address",
    "given_name",
    "surname",
    "full_name",
    "business_name",
    "name_on_card",
    "address_line_1",
    "address_line_2",
    "postal_code",
    "admin_area_1",
    "admin_area_2",
    "payer_id",
    "merchant_id",
    "account_id",
    "payer_email",
    "tax_id",
    "website",
    "logo_url",
}
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_PAYER_LINK_RE = re.compile(r"https://www\.sandbox\.paypal\.com/\S+")


def fingerprint(value: object) -> str:
    return "redacted:" + hashlib.sha256(str(value).encode()).hexdigest()[:12]


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: fingerprint(v) if k.lower() in _SENSITIVE_KEYS else redact(v) for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str) and (_PAYER_LINK_RE.match(obj) or _EMAIL_RE.search(obj)):
        return fingerprint(obj)
    return obj
