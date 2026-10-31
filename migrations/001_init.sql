-- StopShade schema v1: submissions + durable jobs outbox.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$ BEGIN
    CREATE TYPE submission_status AS ENUM ('pending', 'processing', 'completed', 'failed');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS submissions (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reference_id          TEXT NOT NULL UNIQUE
                          CHECK (reference_id ~ '^BUS-[0-9]{4}-[0-9A-HJKMNP-TV-Z]{8}$'),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    email                 TEXT NOT NULL CHECK (char_length(email) <= 254),
    location              JSONB NOT NULL,
    observations          JSONB NOT NULL
                          CHECK (jsonb_typeof(observations) = 'array'
                                 AND jsonb_array_length(observations) >= 1),
    image_key             TEXT NOT NULL,
    image_content_type    TEXT NOT NULL CHECK (image_content_type IN ('image/jpeg', 'image/png')),
    report                JSONB,
    status                submission_status NOT NULL DEFAULT 'pending',
    error_metadata        JSONB,
    receipt_token_hash    TEXT NOT NULL,
    idempotency_key_hash  TEXT UNIQUE,
    CONSTRAINT report_iff_completed CHECK ((status = 'completed') = (report IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS jobs (
    id             BIGSERIAL PRIMARY KEY,
    submission_id  UUID NOT NULL UNIQUE REFERENCES submissions(id) ON DELETE CASCADE,
    attempts       INT NOT NULL DEFAULT 0,
    max_attempts   INT NOT NULL DEFAULT 3,
    next_run_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_by      TEXT,
    locked_until   TIMESTAMPTZ,
    done_at        TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS jobs_ready_idx ON jobs (next_run_at) WHERE done_at IS NULL;
