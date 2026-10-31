"""Worker tests against real PostgreSQL with a STAND-IN model (no live Gemma calls)."""
import json

import pytest

from app.report_schema import ReportValidationError, extract_json, validate_model_output
from app.worker import TransientModelError, Worker
from tests.test_api import EMAIL, OBS, post, rows

GOOD = {
    "image_description": "A sidewalk with a pole-mounted bus stop sign; no bench is in frame.",
    "visual_findings": [
        {"category": "trees_vegetation", "visibility": "not_visible", "description": "No trees in frame."},
        {"category": "missing_seating", "visibility": "not_visible", "description": "No bench seen in this photo."},
    ],
    "identified_issues": [
        {"category": "missing_seating", "description": "Reporter says seating is missing; the photo cannot confirm the whole waiting area.",
         "evidence_source": "user_report", "requires_human_review": True},
    ],
    "recommended_actions": ["Ask the transit operator to review the reported seating condition."],
}


class FakeModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []

    def generate(self, image, mime_type, prompt):
        assert image and mime_type in ("image/jpeg", "image/png")
        self.prompts.append(prompt)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def make_worker(pool, storage, model):
    return Worker(pool, storage, model, "stand-in-model", worker_id="test", sleep=lambda s: None)


def make_due(pool):
    with pool.connection() as conn:
        conn.execute("UPDATE jobs SET next_run_at = now() - interval '1 second'")


def job(pool):
    with pool.connection() as conn:
        return conn.execute("SELECT * FROM jobs").fetchone()


def test_happy_path_same_row(client, pool, storage, jpeg):
    ref = post(client, jpeg).json()["reference_id"]
    [before] = rows(pool)
    model = FakeModel(json.dumps(GOOD))
    assert make_worker(pool, storage, model).run_once() == "completed"

    [after] = rows(pool)
    assert after["id"] == before["id"] and after["reference_id"] == ref
    assert after["status"] == "completed"
    assert after["observations"] == OBS  # never overwritten
    r = after["report"]
    assert r["reference_id"] == ref and r["user_observations"] == OBS
    assert r["location"] == before["location"]
    assert r["responsible_organization"] == {"name": None, "contact": None, "source_url": None, "verified_on": None}
    assert r["model"] == "stand-in-model"
    assert job(pool)["done_at"] is not None
    # privacy: no email or token in the prompt
    assert EMAIL not in model.prompts[0] and "@" not in model.prompts[0]
    assert "missing_seating" in model.prompts[0] and ref in model.prompts[0]


def test_status_endpoint_shows_report(client, pool, storage, jpeg):
    body = post(client, jpeg).json()
    make_worker(pool, storage, FakeModel(json.dumps(GOOD))).run_once()
    r = client.get(f"/api/v1/submissions/{body['reference_id']}", headers={"Authorization": f"Bearer {body['receipt_token']}"})
    assert r.json()["status"] == "completed"
    assert r.json()["report"]["identified_issues"][0]["evidence_source"] == "user_report"


def test_fenced_json_accepted(client, pool, storage, jpeg):
    post(client, jpeg)
    assert make_worker(pool, storage, FakeModel("```json\n" + json.dumps(GOOD) + "\n```")).run_once() == "completed"


def test_invalid_json_retries_then_fails(client, pool, storage, jpeg):
    post(client, jpeg)
    w = make_worker(pool, storage, FakeModel("not json", "{}", '{"image_description": 5}'))
    assert w.run_once() == "retry"
    assert rows(pool)[0]["status"] == "processing"
    make_due(pool)
    assert w.run_once() == "retry"
    make_due(pool)
    assert w.run_once() == "failed"
    [row] = rows(pool)
    assert row["status"] == "failed" and row["report"] is None
    assert row["error_metadata"]["code"] == "INVALID_MODEL_OUTPUT"
    assert row["error_metadata"]["retry_count"] == 3
    assert row["observations"] == OBS
    assert make_worker(pool, storage, FakeModel()).run_once() is None  # nothing left


def test_transient_then_success(client, pool, storage, jpeg):
    post(client, jpeg)
    w = make_worker(pool, storage, FakeModel(TransientModelError("503"), json.dumps(GOOD)))
    assert w.run_once() == "retry"
    assert w.run_once() is None  # backoff not elapsed yet
    make_due(pool)
    assert w.run_once() == "completed"
    [row] = rows(pool)
    assert row["status"] == "completed" and row["error_metadata"] is None


def test_completed_not_overwritten_by_replayed_job(client, pool, storage, jpeg):
    post(client, jpeg)
    make_worker(pool, storage, FakeModel(json.dumps(GOOD))).run_once()
    original = rows(pool)[0]["report"]
    # simulate a replayed/stale job
    with pool.connection() as conn:
        conn.execute("UPDATE jobs SET done_at = NULL, attempts = 0")
    other = dict(GOOD, image_description="SHOULD NOT APPEAR")
    assert make_worker(pool, storage, FakeModel(json.dumps(other))).run_once() is None
    [row] = rows(pool)
    assert row["report"] == original
    assert len(rows(pool)) == 1


def test_crash_lease_recovery(client, pool, storage, jpeg):
    post(client, jpeg)
    w = make_worker(pool, storage, FakeModel(json.dumps(GOOD)))
    claimed = w.claim()  # worker "crashes" right after claiming
    assert claimed and rows(pool)[0]["status"] == "processing"
    assert w.claim() is None  # lease still held
    with pool.connection() as conn:
        conn.execute("UPDATE jobs SET locked_until = now() - interval '1 second'")
    assert w.run_once() == "completed"
    assert len(rows(pool)) == 1


def test_crash_on_last_attempt_marks_failed(client, pool, storage, jpeg):
    post(client, jpeg)
    with pool.connection() as conn:
        conn.execute("UPDATE jobs SET attempts = 3, locked_until = now() - interval '1 second'")
        conn.execute("UPDATE submissions SET status = 'processing'")
    assert make_worker(pool, storage, FakeModel()).run_once() is None
    [row] = rows(pool)
    assert row["status"] == "failed" and row["error_metadata"]["code"] == "PROCESSING_FAILED"


def test_image_missing(client, pool, storage, s3, jpeg):
    post(client, jpeg)
    s3.delete_object(Bucket="civitstop-test", Key=rows(pool)[0]["image_key"])
    assert make_worker(pool, storage, FakeModel()).run_once() == "retry"
    assert rows(pool)[0]["error_metadata"]["code"] == "IMAGE_UNAVAILABLE"


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(extra="x"),
    lambda d: d.pop("recommended_actions"),
    lambda d: d["visual_findings"][0].update(visibility="absent"),
    lambda d: d["visual_findings"][0].update(category="bench"),
    lambda d: d["identified_issues"][0].update(evidence_source="model"),
    lambda d: d["identified_issues"][0].update(requires_human_review="yes"),
    lambda d: d.update(recommended_actions=[""]),
    lambda d: d.update(image_description=""),
])
def test_schema_rejects(mutate):
    data = json.loads(json.dumps(GOOD))
    mutate(data)
    with pytest.raises(ReportValidationError):
        validate_model_output(data)


def test_extract_json_rejects_array():
    with pytest.raises(ReportValidationError):
        extract_json("[1,2]")
