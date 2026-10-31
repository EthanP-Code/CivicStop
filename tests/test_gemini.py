"""Unit tests for the Gemini boundary; no real provider requests."""
import json
from datetime import datetime, timezone

import httpx
import pytest

from app.config import Settings
from app.gemini import InvalidModelOutput, analyze_image

pytestmark = pytest.mark.no_db


def settings() -> Settings:
    return Settings(
        database_url="postgresql://unused",
        spaces_bucket="unused",
        spaces_region="unused",
        spaces_endpoint="https://example.invalid",
        spaces_access_key="unused",
        spaces_secret_key="unused",
        token_hash_secret=b"x" * 32,
        max_image_bytes=5 * 1024 * 1024,
        gemini_api_key="test-key",
    )


SUBMISSION = {
    "reference_id": "BUS-2026-ABCDEFGH",
    "created_at": datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc),
    "location": {"type": "address", "address": "100 Main St"},
    "observations": ["missing_shelter"],
}

ANALYSIS = {
    "image_description": "A marked bus stop beside a road.",
    "visual_findings": [{
        "category": "missing_shelter",
        "visibility": "visible",
        "description": "No shelter is visible beside the stop sign.",
    }],
    "identified_issues": [{
        "category": "missing_shelter",
        "evidence_source": "both",
        "description": "The rider reported no shelter and none is visible.",
        "requires_human_review": True,
    }],
    "recommended_actions": ["Ask the transit agency to review shelter eligibility."],
}


def test_analyze_image_returns_trusted_report():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == "test-key"
        body = json.loads(request.content)
        assert body["contents"][0]["parts"][0]["inlineData"]["mimeType"] == "image/jpeg"
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": json.dumps(ANALYSIS)}]}}]
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = analyze_image(settings(), b"image", "image/jpeg", SUBMISSION, client)

    assert report["reference_id"] == SUBMISSION["reference_id"]
    assert report["location"] == SUBMISSION["location"]
    assert report["user_observations"] == ["missing_shelter"]
    assert report["responsible_organization"]["name"] is None
    assert report["model"] == "gemini-flash-latest"


def test_analyze_image_rejects_invalid_output():
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200,
        json={"candidates": [{"content": {"parts": [{"text": '{"image_description": 4}'}]}}]},
    )))
    with pytest.raises(InvalidModelOutput):
        analyze_image(settings(), b"image", "image/jpeg", SUBMISSION, client)
