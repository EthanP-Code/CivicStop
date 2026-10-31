# CivitStop

Report conditions at a Raleigh bus stop. A rider submits a photo, location, email and what they noticed, and gets a tracking reference and receipt token immediately. A separate background worker then uses Gemma 4 to write a structured, provenance-aware report onto the same record. The rider can view the report on the same page, or later with **Look up a report**.

CivitStop is an AI-assisted observation tool. It is **not** an official city reporting channel, does not file tickets with any agency, and does not send email.

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

Requirements: Python 3.12+, PostgreSQL 15+, and an S3-compatible bucket. For development, a local [moto](https://github.com/getmoto/moto) server stands in for DigitalOcean Spaces, so no cloud account is needed. You do need a Gemini API key for the worker.

### One-time setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt 'moto[server]'

# Database (matches DATABASE_URL in .env.example)
psql -d postgres -c "create role civitstop login password 'localdev'"
createdb -O civitstop civitstop

cp .env.example .env
```

Then fill in `.env` for local development:

```bash
SPACES_REGION=us-east-1
SPACES_ENDPOINT=http://127.0.0.1:9000     # the local moto server
SPACES_ACCESS_KEY=local
SPACES_SECRET_KEY=local
TOKEN_HASH_SECRET=<output of the command below>
GEMINI_API_KEY=<your key>
```

```bash
python3 -c "import secrets,base64;print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

`.env` is gitignored. Never commit real keys.

### Start the app (three terminals)

**1. Local S3 stand-in**

```bash
.venv/bin/moto_server -H 127.0.0.1 -p 9000
```

**2. API.** It creates the bucket, applies any new migrations and starts the server:

```bash
set -a; . ./.env; set +a
.venv/bin/python -c "import boto3,os; boto3.client('s3', endpoint_url=os.environ['SPACES_ENDPOINT'], region_name=os.environ['SPACES_REGION'], aws_access_key_id='local', aws_secret_access_key='local').create_bucket(Bucket=os.environ['SPACES_BUCKET'])"
.venv/bin/python -m app.migrate
.venv/bin/uvicorn app.main:app --port 8080
```

**3. AI worker**

```bash
set -a; . ./.env; set +a
.venv/bin/python -m app.worker
```

Open http://localhost:8080.

- The moto server keeps photos in memory only, so re-run the bucket command after every moto restart. Submissions and reports in PostgreSQL persist.
- If port 8080 or 9000 is busy (`Address already in use`), pick other ports, for example `--port 8081` and `-p 9001`, and update `SPACES_ENDPOINT` to match.
- To process several photos in parallel, start more workers. Each one runs a single job at a time, and the queue gives each job to only one worker.

Check that your key can reach Gemma 4 with one image before running the worker:

```bash
.venv/bin/python scripts/smoke_gemma.py path/to/bus-stop.jpg
```

## Using the app

1. **Submit:** choose a JPEG or PNG photo (up to 5 MB), enter an address or GPS coordinates, your email, and at least one observation.
2. **Save the receipt:** the receipt screen shows a **Reference** (for example `BUS-2026-2PVVX6YZ`) and a **Receipt token**, each with a Copy button. Save both. The token is shown only here, and it can't be recovered.
3. **Wait for the report:** status goes from *Pending* to *Processing* to *Completed*. The page checks automatically for 5 minutes, then shows **Check again**.
4. **View it later:** click **Already submitted? Look up a report** below the form and enter the reference and receipt token.

**How long it takes:** Gemma typically takes 40–170 seconds per photo, depending on how much is in it. One worker handles one photo at a time, so photos submitted close together wait in line.

## API

| Method and path | Purpose |
|---|---|
| `POST /api/v1/submissions` | Multipart fields: `image`, `location` (JSON), `email`, `observations` (JSON array). Optional `Idempotency-Key` header. Returns `202` with `reference_id` and a one-time `receipt_token`. |
| `GET /api/v1/submissions/{reference_id}` | Needs `Authorization: Bearer <receipt_token>`. Returns `status` (`pending`, `processing`, `completed` or `failed`) and, when completed, the `report`. |
| `GET /healthz` | Health check |

Example:

```bash
curl -s -X POST localhost:8080/api/v1/submissions \
  -H "Idempotency-Key: $(uuidgen)" \
  -F "image=@bus-stop.jpg;type=image/jpeg" \
  -F 'location={"type":"address","address":"Moore Square, Raleigh NC"}' \
  -F email=rider@example.com \
  -F 'observations=["missing_seating","signage"]'

curl -s localhost:8080/api/v1/submissions/BUS-2026-XXXXXXXX \
  -H "Authorization: Bearer <receipt_token>" | python3 -m json.tool
```

Observation values: `trees_vegetation`, `missing_shelter`, `missing_seating`, `trash_cans`, `bus_info_posters`, `signage`.

## The report

Each completed report contains:

- `image_description`: what the model sees in the photo.
- `visual_findings`: one entry per observation, with `visibility` set to `visible`, `not_visible`, `unclear` or `not_assessable`.
- `identified_issues`: each with an `evidence_source` (`visual`, `user_report` or `both`) and a `requires_human_review` flag. The model can also raise issues the rider didn't report, under category `other`.
- `recommended_actions`: suggestions only.
- `responsible_organization`: always empty for now, because no verified contact directory exists yet.
- Server-filled fields: reference, submission time, location, the rider's observations, the model name, and the notice that this is an AI-assisted observation, not an official inspection.

**Validation:** before a report is saved, the worker checks its structure in `app/report_schema.py`: exact fields, allowed values, text length and list size. Output that fails these checks is retried, up to 3 attempts, and then the submission is marked `failed` with `INVALID_MODEL_OUTPUT`. These checks don't catch content that contradicts itself, such as a finding marked `not_visible` whose description says the item is visible, so a person should still review reports.

## Configuration

| Variable | Used by | Notes |
|---|---|---|
| `DATABASE_URL` | API, worker | |
| `SPACES_BUCKET`, `SPACES_REGION`, `SPACES_ENDPOINT`, `SPACES_ACCESS_KEY`, `SPACES_SECRET_KEY` | API, worker | Bucket must be private |
| `TOKEN_HASH_SECRET` | API | At least 32 bytes, base64. Keys the receipt-token and idempotency-key hashes. Changing it invalidates every existing receipt token. |
| `MAX_IMAGE_BYTES` | API | Default 5 MB |
| `GEMINI_API_KEY` | Worker | The worker reads only this name. If `GOOGLE_API_KEY` is also set, the worker still uses `GEMINI_API_KEY`, though the Google SDK logs a warning. |
| `GEMMA_MODEL` | Worker | Default `gemma-4-26b-a4b-it` |

Worker tuning constants are in `app/worker.py`:

- `MAX_OUTPUT_TOKENS` (8192): the limit covers Gemma's hidden reasoning ("thinking") as well as the report. With 2048, detailed photos used up the whole limit and returned no text.
- `LEASE_SECONDS` (300): how long a worker holds a job before another worker may take it over.

## Tests

The tests run against a real local PostgreSQL database and a mocked S3 bucket:

```bash
createdb -O civitstop civitstop_test
.venv/bin/pytest -q
```

To use a different database, set `TEST_DATABASE_URL` (default `postgresql://civitstop:localdev@localhost:5432/civitstop_test`).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "Choose a JPEG or PNG photo." for a `.jpg` or `.jpeg` file | The file is probably another format with a JPEG name. Images saved from websites are often WebP, and iPhone photos are often HEIC. Check with `file photo.jpg`, then convert on macOS with `sips -s format jpeg photo.jpg --out photo-fixed.jpg`. |
| Report stuck on *Processing* | Normal for up to about 3 minutes per photo, plus waiting time when other photos are ahead. Check the worker's terminal for `processing reference=…` and `completed reference=…`. |
| Status *Failed* with `INVALID_MODEL_OUTPUT` | The model's answer didn't pass validation. The worker log says why (`not valid JSON`, `keys mismatch`, `model returned no text: finish_reason=MAX_TOKENS`). Submit again. |
| Lookup says "We couldn't find a report…" | The reference or token is wrong. The same message is shown for an unknown reference, by design. |
| Page doesn't show the receipt token | The browser is using an old `app.js`. Hard-refresh with Cmd+Shift+R. |
| Upload fails with a storage error after restarting moto | The bucket is gone. Re-run the bucket command from step 2. |
| `Address already in use` | Another process has the port. Use a different port (see [Start the app](#start-the-app-three-terminals)). |

## Privacy

- Email is stored with the submission and never returned by the API, sent to the model, or used for outbound messages.
- Photos are stored in a private bucket under random keys; the original filename is discarded. Photos are not re-encoded, so EXIF metadata (which may include GPS) stays in the private object.
- Only a keyed hash of each receipt token is stored. A missing or badly formatted token returns `401`. A wrong token and an unknown reference both return the same `404`, so guessing can't reveal whether a reference exists.
- Logs record reference IDs and status codes only.
- Retention/deletion policy: not yet defined. Operators can remove a submission manually (delete the row; the job cascades) and its object from the bucket.

## Known limitations

- A lost receipt token can't be recovered, and no operator or admin view of reports exists.
- Report validation checks structure, not whether the content is consistent.
- No rate limiting or CAPTCHA.
- Only JPEG and PNG are accepted (not WebP or HEIC).
- The page checks for a report automatically for 5 minutes only; after that the rider clicks **Check again**.
- Without an `Idempotency-Key`, duplicate network requests can create separate submissions (the bundled UI always sends one).
