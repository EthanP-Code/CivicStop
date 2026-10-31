"""Reference IDs, receipt tokens, and keyed hashes."""
import base64
import hashlib
import hmac
import re
import secrets
import uuid
from datetime import datetime

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O, U
REFERENCE_RE = re.compile(r"^BUS-[0-9]{4}-[0-9A-HJKMNP-TV-Z]{8}$")
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def new_reference(now: datetime) -> str:
    return f"BUS-{now.year}-" + "".join(secrets.choice(ALPHABET) for _ in range(8))


def hmac_hex(secret: bytes, purpose: str, value: str) -> str:
    return hmac.new(secret, f"{purpose}:{value}".encode(), hashlib.sha256).hexdigest()


def new_receipt_token(secret: bytes, reference_id: str, idem_key: str | None) -> str:
    if idem_key is None:
        return secrets.token_urlsafe(32)
    # Deterministic so a retried POST with the same key can return the same token.
    raw = hmac.new(secret, f"receipt:{reference_id}:{idem_key}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def receipt_hash(secret: bytes, token: str) -> str:
    return hmac_hex(secret, "rt", token)


def idempotency_hash(secret: bytes, key: str) -> str:
    return hmac_hex(secret, "idem", key)


def new_image_key(now: datetime, ext: str) -> str:
    return f"submissions/{now:%Y}/{now:%m}/{uuid.uuid4()}.{ext}"
