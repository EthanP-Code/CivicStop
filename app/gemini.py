"""Gemini Flash image analysis client."""
import base64
import json

import httpx
from pydantic import ValidationError

from .config import Settings
from .report_schema import ModelAnalysis, ResponsibleOrganization, StoredReport

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
REPORT_NOTICE = "AI-assisted observation; not an official inspection or agency submission."


class ModelUnavailable(RuntimeError):
    """The provider could not produce a response right now."""


class InvalidModelOutput(RuntimeError):
    """The provider response was present but unsafe or invalid."""


def _prompt(location: dict, observations: list[str]) -> str:
    return f"""Analyze this bus-stop photo for the StopShade civic observation tool.

The rider-reported location is {json.dumps(location, separators=(',', ':'))}.
The rider selected these observations: {json.dumps(observations)}.

Describe only what the image supports. A rider selection is not visual proof. For each
issue, use evidence_source "visual" only when visible in the photo, "user_report" only
when it comes solely from the rider selection, and "both" when both support it. Use
"unclear" or "not_assessable" instead of guessing. Do not identify people, infer
sensitive traits, invent agency contacts, or claim an official inspection occurred.
Return concise, accessible language in the requested JSON schema."""


def _response_text(payload: dict) -> str:
    try:
        parts = payload["candidates"][0]["content"]["parts"]
        text = "".join(part.get("text", "") for part in parts).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise InvalidModelOutput("Gemini response contained no candidate text") from exc
    if not text:
        raise InvalidModelOutput("Gemini response contained empty candidate text")
    return text


def analyze_image(
    settings: Settings,
    image: bytes,
    content_type: str,
    submission: dict,
    client: httpx.Client | None = None,
) -> dict:
    """Generate and validate a report. The API key is sent only in a header."""
    if not settings.gemini_api_key:
        raise ModelUnavailable("GEMINI_API_KEY is not configured")

    request_body = {
        "contents": [{
            "parts": [
                {"inlineData": {
                    "mimeType": content_type,
                    "data": base64.b64encode(image).decode("ascii"),
                }},
                {"text": _prompt(submission["location"], submission["observations"])},
            ]
        }],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 2048,
            "responseMimeType": "application/json",
            "responseJsonSchema": ModelAnalysis.model_json_schema(),
        },
    }
    owns_client = client is None
    client = client or httpx.Client(timeout=settings.gemini_timeout_seconds)
    try:
        response = client.post(
            f"{API_ROOT}/{settings.gemini_model}:generateContent",
            headers={"Content-Type": "application/json", "X-goog-api-key": settings.gemini_api_key},
            json=request_body,
        )
        response.raise_for_status()
        analysis = ModelAnalysis.model_validate_json(_response_text(response.json()))
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise ModelUnavailable("Gemini request failed") from exc
    except ValidationError as exc:
        raise InvalidModelOutput("Gemini returned invalid report JSON") from exc
    finally:
        if owns_client:
            client.close()

    report = StoredReport(
        report_notice=REPORT_NOTICE,
        reference_id=submission["reference_id"],
        submitted_at=submission["created_at"].isoformat(),
        location=submission["location"],
        user_observations=submission["observations"],
        **analysis.model_dump(),
        responsible_organization=ResponsibleOrganization(),
        model=settings.gemini_model,
    )
    return report.model_dump(mode="json")
