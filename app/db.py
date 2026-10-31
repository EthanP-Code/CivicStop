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


def ping(pool: ConnectionPool) -> bool:
    with pool.connection() as conn:
        return conn.execute("SELECT 1 AS ok").fetchone()["ok"] == 1


def iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
