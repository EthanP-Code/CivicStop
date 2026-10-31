"""Background worker: claims jobs, asks Gemma 4 about the photo, saves the report on the same row.

Run: python -m app.worker
"""
import logging
import os
import signal
import socket
import time
import uuid
from datetime import timezone

from psycopg.types.json import Jsonb

from . import db
from .config import load_settings
from .report_schema import ReportValidationError, assemble_report, extract_json, validate_model_output
from .storage import Storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("civitstop.worker")

DEFAULT_MODEL = "gemma-4-26b-a4b-it"
LEASE_SECONDS = 300  # real photos can take 60-90s+ with Gemma's thinking
MAX_OUTPUT_TOKENS = 8192  # includes Gemma's thinking tokens; 2048 truncated before any answer
IDLE_SLEEP = 2.0
BACKOFF_BASE = 5  # seconds; retry n waits BACKOFF_BASE * 2**(n-1)

LABELS = {
    "trees_vegetation": "Trees / vegetation",
    "missing_shelter": "Missing bus shelter",
    "missing_seating": "Missing seating",
    "trash_cans": "Trash cans",
    "bus_info_posters": "Bus information posters",
    "signage": "Signage",
}

PROMPT = """You are helping a community member document conditions at a public bus stop.
Look carefully at the attached photo and return ONLY a JSON object, no prose, no markdown.

Context from the reporter (these are CLAIMS, not verified facts):
- Reference: {reference_id}
- Submitted at: {submitted_at}
- Location as entered by the reporter: {location}
- Observations the reporter selected: {observations}

Rules:
- Describe only what is actually visible. Do not guess about anything outside the frame.
- "not_visible" means "not seen in this photo", never "confirmed absent".
- Treat the reporter's selections as user reports. If the photo cannot confirm one, say so and
  use evidence_source "user_report". Use "visual" only for what the photo shows on its own,
  and "both" when the photo supports the reporter's claim.
- Set requires_human_review to true whenever the photo cannot settle the issue.
- Do not name agencies, people, phone numbers, emails or URLs. Do not claim heat, shade
  measurements, ADA compliance or structural safety.
- Keep each description under 40 words. At most 6 items per list.

Allowed category values: trees_vegetation, missing_shelter, missing_seating, trash_cans,
bus_info_posters, signage, other.
Allowed visibility values: visible, not_visible, unclear, not_assessable.
Allowed evidence_source values: visual, user_report, both.

Return exactly this shape:
{{
  "image_description": "string",
  "visual_findings": [{{"category": "...", "visibility": "...", "description": "string"}}],
  "identified_issues": [{{"category": "...", "description": "string", "evidence_source": "...", "requires_human_review": true}}],
  "recommended_actions": ["string"]
}}
Include one visual_findings entry for each category the reporter selected."""


class TransientModelError(Exception):
    """Provider/network trouble worth retrying."""


class GemmaClient:
    """Thin wrapper over the Gemini API for a Gemma 4 model with image input."""

    def __init__(self, api_key: str, model: str):
        from google import genai  # imported lazily so tests don't need it

        self._genai = genai
        self.client = genai.Client(api_key=api_key)
        self.model = model

    def generate(self, image: bytes, mime_type: str, prompt: str) -> str:
        from google.genai import errors, types

        try:
            resp = self.client.models.generate_content(
                model=self.model,
                contents=[types.Part.from_bytes(data=image, mime_type=mime_type), prompt],
                config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=MAX_OUTPUT_TOKENS),
            )
        except errors.ClientError as exc:  # 4xx: retrying the same request rarely helps, except 429
            if getattr(exc, "code", None) == 429:
                raise TransientModelError("rate limited") from exc
            raise
        except Exception as exc:
            raise TransientModelError(type(exc).__name__) from exc
        if not resp.text and resp.candidates:
            log.warning("model returned no text: finish_reason=%s", resp.candidates[0].finish_reason)
        return resp.text or ""


def build_prompt(sub: dict) -> str:
    loc = sub["location"]
    location = loc["address"] if loc.get("type") == "address" else f"GPS {loc['latitude']}, {loc['longitude']}"
    observations = ", ".join(f"{c} ({LABELS.get(c, c)})" for c in sub["observations"])
    return PROMPT.format(
        reference_id=sub["reference_id"],
        submitted_at=db.iso(sub["created_at"]),
        location=location,
        observations=observations,
    )


# ---------- queue SQL ----------

CLAIM_SQL = """
UPDATE jobs SET locked_by = %(worker)s,
                locked_until = now() + make_interval(secs => %(lease)s),
                attempts = attempts + 1
WHERE id = (
    SELECT j.id FROM jobs j
    JOIN submissions s ON s.id = j.submission_id
    WHERE j.done_at IS NULL
      AND j.next_run_at <= now()
      AND (j.locked_until IS NULL OR j.locked_until < now())
      AND j.attempts < j.max_attempts
      AND s.status IN ('pending', 'processing')
    ORDER BY j.next_run_at
    FOR UPDATE OF j SKIP LOCKED
    LIMIT 1
)
RETURNING id, submission_id, attempts, max_attempts
"""

# Jobs whose final attempt crashed (lease expired, attempts exhausted) get a truthful terminal state.
SWEEP_SQL = """
WITH dead AS (
    UPDATE jobs SET done_at = now(), locked_by = NULL, locked_until = NULL
    WHERE done_at IS NULL AND attempts >= max_attempts
      AND (locked_until IS NULL OR locked_until < now())
    RETURNING submission_id, attempts
)
UPDATE submissions s
SET status = 'failed', updated_at = now(),
    error_metadata = jsonb_build_object('code', 'PROCESSING_FAILED', 'retry_count', d.attempts,
                                        'last_attempt_at', now())
FROM dead d
WHERE s.id = d.submission_id AND s.status <> 'completed'
"""


class Worker:
    def __init__(self, pool, storage, model_client, model_name: str, worker_id: str | None = None,
                 sleep=time.sleep):
        self.pool = pool
        self.storage = storage
        self.model = model_client
        self.model_name = model_name
        self.worker_id = worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
        self.sleep = sleep
        self.running = True

    def claim(self) -> dict | None:
        with self.pool.connection() as conn:
            with conn.transaction():
                job = conn.execute(CLAIM_SQL, {"worker": self.worker_id, "lease": LEASE_SECONDS}).fetchone()
                if not job:
                    return None
                sub = conn.execute(
                    """
                    UPDATE submissions SET status = 'processing', updated_at = now()
                    WHERE id = %s AND status IN ('pending', 'processing')
                    RETURNING id, reference_id, created_at, location, observations,
                              image_key, image_content_type
                    """,  # email and token hash are deliberately not selected
                    (job["submission_id"],),
                ).fetchone()
                if not sub:
                    conn.execute("UPDATE jobs SET done_at = now() WHERE id = %s", (job["id"],))
                    return None
                return {"job": job, "sub": sub}

    def sweep(self) -> None:
        with self.pool.connection() as conn:
            conn.execute(SWEEP_SQL)

    def complete(self, job: dict, sub: dict, report: dict) -> bool:
        with self.pool.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    UPDATE submissions SET report = %s, status = 'completed', error_metadata = NULL,
                                           updated_at = now()
                    WHERE id = %s AND status <> 'completed'
                    RETURNING id
                    """,
                    (Jsonb(report), sub["id"]),
                ).fetchone()
                conn.execute(
                    "UPDATE jobs SET done_at = now(), locked_by = NULL, locked_until = NULL WHERE id = %s",
                    (job["id"],),
                )
                return row is not None

    def fail_or_retry(self, job: dict, sub: dict, code: str) -> str:
        attempts, max_attempts = job["attempts"], job["max_attempts"]
        meta = {"code": code, "retry_count": attempts}
        with self.pool.connection() as conn:
            with conn.transaction():
                if attempts >= max_attempts:
                    conn.execute(
                        "UPDATE jobs SET done_at = now(), locked_by = NULL, locked_until = NULL WHERE id = %s",
                        (job["id"],),
                    )
                    conn.execute(
                        """
                        UPDATE submissions SET status = 'failed', updated_at = now(),
                               error_metadata = %s::jsonb || jsonb_build_object('last_attempt_at', now())
                        WHERE id = %s AND status <> 'completed'
                        """,
                        (Jsonb(meta), sub["id"]),
                    )
                    return "failed"
                delay = BACKOFF_BASE * 2 ** (attempts - 1)
                conn.execute(
                    """
                    UPDATE jobs SET locked_by = NULL, locked_until = NULL,
                                    next_run_at = now() + make_interval(secs => %s)
                    WHERE id = %s
                    """,
                    (delay, job["id"]),
                )
                conn.execute(
                    """
                    UPDATE submissions SET updated_at = now(),
                           error_metadata = %s::jsonb || jsonb_build_object('last_attempt_at', now())
                    WHERE id = %s AND status <> 'completed'
                    """,
                    (Jsonb(meta), sub["id"]),
                )
                return "retry"

    def process(self, claimed: dict) -> str:
        job, sub = claimed["job"], claimed["sub"]
        ref = sub["reference_id"]
        log.info("processing reference=%s attempt=%s", ref, job["attempts"])
        try:
            image = self.storage.get_image(sub["image_key"])
        except Exception as exc:
            log.warning("image fetch failed reference=%s: %s", ref, type(exc).__name__)
            return self.fail_or_retry(job, sub, "IMAGE_UNAVAILABLE")

        started = time.perf_counter()
        try:
            raw = self.model.generate(image, sub["image_content_type"], build_prompt(sub))
        except TransientModelError as exc:
            log.warning("model call failed reference=%s: %s", ref, exc)
            return self.fail_or_retry(job, sub, "MODEL_UNAVAILABLE")
        except Exception as exc:
            log.warning("model call failed reference=%s: %s", ref, type(exc).__name__)
            return self.fail_or_retry(job, sub, "MODEL_UNAVAILABLE")
        log.info("model responded reference=%s in %.1fs", ref, time.perf_counter() - started)

        try:
            model_part = validate_model_output(extract_json(raw))
        except ReportValidationError as exc:
            log.warning("invalid model output reference=%s: %s", ref, exc)
            return self.fail_or_retry(job, sub, "INVALID_MODEL_OUTPUT")

        report = assemble_report(
            model_part,
            reference_id=ref,
            submitted_at=db.iso(sub["created_at"].astimezone(timezone.utc)),
            location=sub["location"],
            observations=sub["observations"],
            model=self.model_name,
        )
        saved = self.complete(job, sub, report)
        log.info("completed reference=%s saved=%s", ref, saved)
        return "completed" if saved else "already_completed"

    def run_once(self) -> str | None:
        self.sweep()
        claimed = self.claim()
        if not claimed:
            return None
        return self.process(claimed)

    def run_forever(self) -> None:
        log.info("worker %s started, model=%s", self.worker_id, self.model_name)
        while self.running:
            try:
                if self.run_once() is None:
                    self.sleep(IDLE_SLEEP)
            except Exception as exc:
                log.error("worker loop error: %s", type(exc).__name__)
                self.sleep(IDLE_SLEEP)
        log.info("worker stopped")


def main() -> None:
    settings = load_settings()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY is required for the worker")
    model_name = os.environ.get("GEMMA_MODEL", DEFAULT_MODEL)
    pool = db.make_pool(settings.database_url)
    worker = Worker(pool, Storage(settings), GemmaClient(api_key, model_name), model_name)

    def stop(*_):
        worker.running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        worker.run_forever()
    finally:
        pool.close()


if __name__ == "__main__":
    main()
