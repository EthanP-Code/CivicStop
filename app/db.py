"""PostgreSQL access: connection pool and the SQL the API needs."""
from dataclasses import dataclass
from datetime import datetime, timezone

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool


def make_pool(database_url: str) -> ConnectionPool:
    pool = ConnectionPool(
        database_url, min_size=1, max_size=5, kwargs={"row_factory": dict_row}, open=False
    )
    pool.open(wait=True, timeout=10)
    return pool


@dataclass
class NewSubmission:
    reference_id: str
    email: str
    location: dict
    observations: list[str]
    image_key: str
    image_content_type: str
    receipt_token_hash: str
    idempotency_key_hash: str | None


def find_by_idempotency(pool: ConnectionPool, idem_hash: str) -> dict | None:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT reference_id, status::text AS status FROM submissions "
            "WHERE idempotency_key_hash = %s",
            (idem_hash,),
        ).fetchone()


def insert_submission(pool: ConnectionPool, s: NewSubmission) -> dict | None:
    """Insert submission + job in one transaction.

    Returns the new row, or None if a concurrent request with the same
    idempotency key committed first.
    """
    with pool.connection() as conn:
        with conn.transaction():
            row = conn.execute(
                """
                INSERT INTO submissions (reference_id, email, location, observations, image_key,
                                         image_content_type, receipt_token_hash, idempotency_key_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key_hash) DO NOTHING
                RETURNING id, reference_id, created_at, status::text AS status
                """,
                (
                    s.reference_id,
                    s.email,
                    Jsonb(s.location),
                    Jsonb(s.observations),
                    s.image_key,
                    s.image_content_type,
                    s.receipt_token_hash,
                    s.idempotency_key_hash,
                ),
            ).fetchone()
            if row is None:
                return None
            conn.execute("INSERT INTO jobs (submission_id) VALUES (%s)", (row["id"],))
            return row


def get_submission(pool: ConnectionPool, reference_id: str) -> dict | None:
    with pool.connection() as conn:
        return conn.execute(
            """
            SELECT reference_id, created_at, updated_at, status::text AS status, location,
                   observations, report, error_metadata, receipt_token_hash
            FROM submissions WHERE reference_id = %s
            """,
            (reference_id,),
        ).fetchone()


def claim_job(pool: ConnectionPool, worker_id: str, lease_seconds: int) -> dict | None:
    """Claim one ready job without blocking other workers."""
    with pool.connection() as conn:
        with conn.transaction():
            job = conn.execute(
                """
                UPDATE jobs SET locked_by = %s,
                                locked_until = now() + (%s * interval '1 second'),
                                attempts = attempts + 1
                WHERE id = (
                    SELECT j.id FROM jobs j
                    JOIN submissions s ON s.id = j.submission_id
                    WHERE j.done_at IS NULL
                      AND j.next_run_at <= now()
                      AND (j.locked_until IS NULL OR j.locked_until < now())
                      AND j.attempts < j.max_attempts
                      AND s.status IN ('pending', 'processing')
                    ORDER BY j.next_run_at, j.id
                    FOR UPDATE OF j SKIP LOCKED
                    LIMIT 1
                )
                RETURNING id AS job_id, submission_id, attempts, max_attempts
                """,
                (worker_id, lease_seconds),
            ).fetchone()
            if job is None:
                return None
            conn.execute(
                """
                UPDATE submissions SET status = 'processing', updated_at = now()
                WHERE id = %s AND status = 'pending'
                """,
                (job["submission_id"],),
            )
            submission = conn.execute(
                """
                SELECT id, reference_id, created_at, location, observations,
                       image_key, image_content_type
                FROM submissions WHERE id = %s
                """,
                (job["submission_id"],),
            ).fetchone()
            return {**submission, **job}


def complete_job(pool: ConnectionPool, job_id: int, submission_id, report: dict) -> None:
    """Persist a validated report and finish the job atomically."""
    with pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                """
                UPDATE submissions
                SET status = 'completed', report = %s, error_metadata = NULL, updated_at = now()
                WHERE id = %s AND status <> 'completed'
                """,
                (Jsonb(report), submission_id),
            )
            conn.execute(
                """
                UPDATE jobs
                SET done_at = now(), locked_by = NULL, locked_until = NULL
                WHERE id = %s
                """,
                (job_id,),
            )


def fail_or_retry_job(
    pool: ConnectionPool,
    job_id: int,
    submission_id,
    attempts: int,
    max_attempts: int,
    code: str,
) -> bool:
    """Release a failed job for retry, or permanently fail it.

    Returns True when no attempts remain.
    """
    exhausted = attempts >= max_attempts
    delay_seconds = min(300, 5 * (2 ** max(0, attempts - 1)))
    with pool.connection() as conn:
        with conn.transaction():
            if exhausted:
                conn.execute(
                    """
                    UPDATE submissions
                    SET status = 'failed', report = NULL,
                        error_metadata = %s, updated_at = now()
                    WHERE id = %s AND status <> 'completed'
                    """,
                    (Jsonb({"code": code}), submission_id),
                )
                conn.execute(
                    """
                    UPDATE jobs
                    SET done_at = now(), locked_by = NULL, locked_until = NULL
                    WHERE id = %s
                    """,
                    (job_id,),
                )
            else:
                conn.execute(
                    """
                    UPDATE jobs
                    SET next_run_at = now() + (%s * interval '1 second'),
                        locked_by = NULL, locked_until = NULL
                    WHERE id = %s
                    """,
                    (delay_seconds, job_id),
                )
    return exhausted


def ping(pool: ConnectionPool) -> bool:
    with pool.connection() as conn:
        return conn.execute("SELECT 1 AS ok").fetchone()["ok"] == 1


def iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
