"""Server-side validation for every submission field."""
import io
import json
import math
import re

from email_validator import EmailNotValidError, validate_email
from PIL import Image

from .config import ADDRESS_MAX, EMAIL_MAX, OBSERVATION_CATEGORIES
from .errors import ApiError, invalid

Image.MAX_IMAGE_PIXELS = 40_000_000  # decompression-bomb guard

CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

MSG_EMAIL = "Enter a valid email address, like name@example.com."
MSG_ADDRESS = "Enter the bus stop address or nearest intersection."
MSG_GPS = "Enter a valid latitude and longitude, or use an address."
MSG_OBS = "Choose at least one observation."
MSG_IMAGE_TYPE = "Choose a JPEG or PNG photo."


def validate_email_field(raw: str | None) -> str:
    value = (raw or "").strip()
    if not 3 <= len(value) <= EMAIL_MAX:
        raise invalid("email", MSG_EMAIL)
    try:
        result = validate_email(value, check_deliverability=False)
    except EmailNotValidError:
        raise invalid("email", MSG_EMAIL)
    normalized = result.normalized
    if len(normalized) > EMAIL_MAX:
        raise invalid("email", MSG_EMAIL)
    return normalized


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate_location(raw: str | None) -> dict:
    try:
        data = json.loads(raw or "")
    except (ValueError, TypeError):
        raise invalid("location", MSG_ADDRESS)
    if not isinstance(data, dict):
        raise invalid("location", MSG_ADDRESS)
    kind = data.get("type")
    if kind == "address":
        if set(data) != {"type", "address"} or not isinstance(data["address"], str):
            raise invalid("location", MSG_ADDRESS)
        address = data["address"].strip()
        if not 1 <= len(address) <= ADDRESS_MAX or CONTROL_CHARS.search(address):
            raise invalid("location", MSG_ADDRESS)
        return {"type": "address", "address": address}
    if kind == "gps":
        if set(data) != {"type", "latitude", "longitude"}:
            raise invalid("location", MSG_GPS)
        lat, lng = data["latitude"], data["longitude"]
        if not (_is_number(lat) and _is_number(lng)):
            raise invalid("location", MSG_GPS)
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise invalid("location", MSG_GPS)
        return {"type": "gps", "latitude": round(float(lat), 6), "longitude": round(float(lng), 6)}
    raise invalid("location", MSG_ADDRESS)


def validate_observations(raw: str | None) -> list[str]:
    try:
        data = json.loads(raw or "")
    except (ValueError, TypeError):
        raise invalid("observations", MSG_OBS)
    if not isinstance(data, list) or not 1 <= len(data) <= len(OBSERVATION_CATEGORIES):
        raise invalid("observations", MSG_OBS)
    if not all(isinstance(v, str) and v in OBSERVATION_CATEGORIES for v in data):
        raise invalid("observations", "Choose only from the listed observations.")
    if len(set(data)) != len(data):
        raise invalid("observations", "Each observation can be chosen only once.")
    return list(data)


def sniff_image(data: bytes, declared_type: str | None) -> str:
    """Return the real content type of a JPEG/PNG, or raise."""
    if data.startswith(JPEG_MAGIC):
        sniffed, pil_format = "image/jpeg", "JPEG"
    elif data.startswith(PNG_MAGIC):
        sniffed, pil_format = "image/png", "PNG"
    else:
        raise ApiError(415, "UNSUPPORTED_MEDIA", MSG_IMAGE_TYPE, "image")
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format
            img.verify()
    except Exception:
        raise ApiError(415, "UNSUPPORTED_MEDIA", MSG_IMAGE_TYPE, "image")
    if fmt != pil_format:
        raise ApiError(415, "UNSUPPORTED_MEDIA", MSG_IMAGE_TYPE, "image")
    declared = (declared_type or "").split(";")[0].strip().lower()
    if declared == "image/jpg":
        declared = "image/jpeg"
    if declared and declared not in ("application/octet-stream",) and declared != sniffed:
        raise ApiError(415, "UNSUPPORTED_MEDIA", MSG_IMAGE_TYPE, "image")
    return sniffed
