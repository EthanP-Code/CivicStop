"""Environment configuration and shared constants."""
import base64
import logging
import os
from dataclasses import dataclass

log = logging.getLogger("civitstop.config")

OBSERVATION_CATEGORIES = (
    "trees_vegetation",
    "missing_shelter",
    "missing_seating",
    "trash_cans",
    "bus_info_posters",
    "signage",
)
ADDRESS_MAX = 256
EMAIL_MAX = 254
ALLOWED_IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png"}

REQUIRED_VARS = (
    "DATABASE_URL",
    "SPACES_BUCKET",
    "SPACES_REGION",
    "SPACES_ENDPOINT",
    "SPACES_ACCESS_KEY",
    "SPACES_SECRET_KEY",
    "TOKEN_HASH_SECRET",
)


@dataclass(frozen=True)
class Settings:
    database_url: str
    spaces_bucket: str
    spaces_region: str
    spaces_endpoint: str
    spaces_access_key: str
    spaces_secret_key: str
    token_hash_secret: bytes
    max_image_bytes: int


def _decode_secret(raw: str) -> bytes:
    try:
        value = base64.b64decode(raw, validate=True)
    except Exception:
        value = raw.encode()
    if len(value) < 32:
        raise RuntimeError("TOKEN_HASH_SECRET must be at least 32 bytes")
    return value


def load_settings() -> Settings:
    missing = [name for name in REQUIRED_VARS if not os.environ.get(name)]
    if missing:
        for name in missing:
            log.error("missing required environment variable: %s", name)
        raise RuntimeError("missing required environment variables: " + ", ".join(missing))
    return Settings(
        database_url=os.environ["DATABASE_URL"],
        spaces_bucket=os.environ["SPACES_BUCKET"],
        spaces_region=os.environ["SPACES_REGION"],
        spaces_endpoint=os.environ["SPACES_ENDPOINT"],
        spaces_access_key=os.environ["SPACES_ACCESS_KEY"],
        spaces_secret_key=os.environ["SPACES_SECRET_KEY"],
        token_hash_secret=_decode_secret(os.environ["TOKEN_HASH_SECRET"]),
        max_image_bytes=int(os.environ.get("MAX_IMAGE_BYTES", "5242880")),
    )
