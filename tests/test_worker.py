"""Worker integration tests using PostgreSQL and the mocked S3 fixture."""
from app import db
from app.gemini import ModelUnavailable
from app.worker import process_one


def seed(pool, reference="BUS-2026-ABCDEFGH"):
    return db.insert_submission(pool, db.NewSubmission(
        reference_id=reference,
        email="rider@example.com",
        location={"type": "address", "address": "100 Main St"},
        observations=["missing_shelter"],
        image_key="submissions/image.jpg",
        image_content_type="image/jpeg",
        receipt_token_hash="a" * 64,
        idempotency_key_hash=None,
    ))


def test_worker_completes_job(settings, pool, storage):
    seed(pool)
    storage.put_image("submissions/image.jpg", b"image", "image/jpeg")

    def analyzer(settings, image, content_type, job):
        assert image == b"image"
        return {"schema_version": "1", "image_description": "fixture"}

    assert process_one(settings, pool, storage, "test-worker", analyzer)
    row = db.get_submission(pool, "BUS-2026-ABCDEFGH")
    assert row["status"] == "completed"
    assert row["report"]["image_description"] == "fixture"
    with pool.connection() as conn:
        job = conn.execute("SELECT done_at FROM jobs").fetchone()
    assert job["done_at"] is not None


def test_worker_releases_failed_attempt(settings, pool, storage):
    seed(pool)
    storage.put_image("submissions/image.jpg", b"image", "image/jpeg")

    def unavailable(*args):
        raise ModelUnavailable("temporary")

    assert process_one(settings, pool, storage, "test-worker", unavailable)
    row = db.get_submission(pool, "BUS-2026-ABCDEFGH")
    assert row["status"] == "processing"
    with pool.connection() as conn:
        job = conn.execute(
            "SELECT attempts, done_at, locked_by, next_run_at > now() AS delayed FROM jobs"
        ).fetchone()
    assert job == {"attempts": 1, "done_at": None, "locked_by": None, "delayed": True}
