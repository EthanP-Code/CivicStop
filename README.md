# StopShade

Report conditions at a Raleigh bus stop. A rider submits a photo, location, email and what they noticed, and gets a tracking reference immediately. A separate background worker then uses Gemma 4 to write a structured, provenance-aware report onto the same record.

StopShade is an AI-assisted observation tool. It is **not** an official city reporting channel, does not file tickets with any agency, and does not send email.

## Architecture

```
Browser form ──POST──▶ FastAPI ──▶ Spaces (private image)
                         │
                         └──▶ PostgreSQL: submissions row (pending) + jobs row, one transaction
                         ◀── 202 + reference ID + receipt token (never waits for AI)

Worker (separate process) ──claims job──▶ Gemma 4 ──▶ validated report on the same row
Browser ──GET with receipt token──▶ status / report
```

## Run locally

Requirements: Python 3.12+, PostgreSQL 15+, and an S3-compatible bucket (DigitalOcean Spaces, or a local stand-in for development).

```bash
pip install -r requirements-dev.txt
cp .env.example .env        # fill in values
set -a; . ./.env; set +a
python -m app.migrate
uvicorn app.main:app --port 8080
```

Open http://localhost:8080.

Tests run against a real local PostgreSQL database and a mocked S3 bucket:

```bash
createdb stopshade_test
TEST_DATABASE_URL=postgresql://USER:PASS@localhost:5432/stopshade_test pytest -q
```

## Configuration

| Variable | Used by | Notes |
|---|---|---|
| `DATABASE_URL` | API, worker | |
| `SPACES_BUCKET`, `SPACES_REGION`, `SPACES_ENDPOINT`, `SPACES_ACCESS_KEY`, `SPACES_SECRET_KEY` | API, worker | bucket must be private |
| `TOKEN_HASH_SECRET` | API | ≥32 bytes, base64; keys the receipt-token and idempotency-key hashes |
| `MAX_IMAGE_BYTES` | API | default 5 MB |
| `GEMINI_API_KEY` | worker | |

## API

- `POST /api/v1/submissions` (multipart: `image`, `location` JSON, `email`, `observations` JSON array; optional `Idempotency-Key` header) returns `202` with `reference_id` and a one-time `receipt_token`.
- `GET /api/v1/submissions/{reference_id}` with `Authorization: Bearer <receipt_token>` returns status and, when completed, the report.
- `GET /healthz`

## Privacy

- Email is stored with the submission and never returned by the API, sent to the model, or used for outbound messages.
- Photos are stored in a private bucket under random keys; the original filename is discarded. Photos are not re-encoded, so EXIF metadata (which may include GPS) stays in the private object.
- Only a keyed hash of each receipt token is stored. A wrong token and an unknown reference return the same 404.
- Logs record reference IDs and status codes only.
- Retention/deletion policy: not yet defined. Operators can remove a submission manually (delete the row; the job cascades) and its object from the bucket.

## Known limitations

No rate limiting or CAPTCHA. A lost receipt token can't be recovered. Without an `Idempotency-Key`, duplicate network requests can create separate submissions (the bundled UI always sends one).
