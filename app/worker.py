"""Durable background worker for Gemini-assisted report generation."""
import logging
import os
import socket
import time
import uuid
from collections.abc import Callable

from . import db
from .config import Settings, load_settings
from .gemini import InvalidModelOutput, ModelUnavailable, analyze_image
from .storage import Storage

log = logging.getLogger("stopshade.worker")


def process_one(
    settings: Settings,
    pool,
    storage: Storage,
    worker_id: str,
    analyzer: Callable = analyze_image,
) -> bool:
    """Process at most one job. Return False when no job is ready."""
    job = db.claim_job(pool, worker_id, settings.worker_lease_seconds)
    if job is None:
        return False

    code = "MODEL_UNAVAILABLE"
    try:
        try:
            image = storage.get_image(job["image_key"])
        except Exception as exc:
            code = "IMAGE_UNAVAILABLE"
            raise ModelUnavailable("image download failed") from exc
        report = analyzer(settings, image, job["image_content_type"], job)
        db.complete_job(pool, job["job_id"], job["submission_id"], report)
        log.info("completed report reference=%s", job["reference_id"])
    except InvalidModelOutput:
        code = "INVALID_MODEL_OUTPUT"
        exhausted = db.fail_or_retry_job(
            pool, job["job_id"], job["submission_id"], job["attempts"], job["max_attempts"], code
        )
        log.warning("invalid model output reference=%s exhausted=%s", job["reference_id"], exhausted)
    except Exception as exc:
        exhausted = db.fail_or_retry_job(
            pool, job["job_id"], job["submission_id"], job["attempts"], job["max_attempts"], code
        )
        log.warning(
            "worker attempt failed reference=%s error=%s exhausted=%s",
            job["reference_id"], type(exc).__name__, exhausted,
        )
    return True


def run() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    settings = load_settings()
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is required by the worker")
    pool = db.make_pool(settings.database_url)
    storage = Storage(settings)
    worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    log.info("worker started id=%s model=%s", worker_id, settings.gemini_model)
    try:
        while True:
            if not process_one(settings, pool, storage, worker_id):
                time.sleep(settings.worker_poll_seconds)
    finally:
        pool.close()


if __name__ == "__main__":
    run()
