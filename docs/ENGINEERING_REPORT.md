# Engineering report — CivitStop

Status as of 2026-10-31, ~13:12 ET. API, UI and worker are built and tested locally. **No live Gemma 4 call has succeeded yet** and nothing is deployed to DigitalOcean.

## Feature status

| Feature | Status | Evidence |
|---|---|---|
| Browser form (photo, address/GPS, email, 6 categories) | **LIVE (local)** | Headless Chrome run below |
| Client validation + accessible error summary | **LIVE (local)** | Headless Chrome run below |
| `POST /api/v1/submissions` → 202 before any AI | **LIVE (local)** | curl + tests |
| Private image storage | **LIVE against a local S3 stand-in (moto)** — not DigitalOcean Spaces | |
| PostgreSQL submission row + durable job, one transaction | **LIVE (local PostgreSQL 16.15)** | psql output below |
| Receipt token auth on `GET /api/v1/submissions/{ref}` | **LIVE (local)** | tests + curl |
| Idempotency (`Idempotency-Key`) incl. concurrent duplicates | **LIVE (local)** | tests |
| Status polling + report rendering in UI | **LIVE (local)**; report content shown was a **FIXTURE** | Headless Chrome run below |
| Worker: atomic claim with lease, same-row update, retries with backoff, terminal failure, lease recovery | **LIVE (local)** against a **STAND-IN model** | `tests/test_worker.py` |
| Strict report-schema validation; server fills reference, time, location, observations, contact | **LIVE (local)** | `tests/test_worker.py` |
| Real Gemma 4 image call (`gemma-4-26b-a4b-it` via Gemini API) | **NOT VERIFIED.** Build sandbox network policy blocks `generativelanguage.googleapis.com` (proxy 403 on CONNECT; the key was never sent to Google). Run `scripts/smoke_gemma.py` from a machine with normal network access. | |
| DigitalOcean App Platform / Managed PostgreSQL / Spaces | **NOT IMPLEMENTED** (no credentials provided yet) | |
| Verified organization contact directory | **NOT IMPLEMENTED**; UI shows "Contact not verified." | |

## Where things ran

All local in a Linux sandbox: PostgreSQL 16.15, API via `uvicorn` on 127.0.0.1:8080, S3 stand-in via `moto_server` on :5055. **Nothing has been deployed and nothing is public.**

Tables: `submissions`, `jobs`, `schema_migrations`.

## Executed commands and results

### Migration
```
$ python -m app.migrate
applied: 001_init.sql
```

### Automated tests (real PostgreSQL, mocked S3)
```
$ python -m pytest -q
65 passed in 8.19s
```
(47 API tests + 18 worker/schema tests.)
Covered: happy path (address+JPEG, GPS+PNG), unique references, idempotent replay, 4 concurrent same-key POSTs → one row/one job/one object, each missing field → 422 with correct `field`, bad observations (empty, duplicate, unknown, non-array, non-string), bad locations (10 cases incl. 257-char address, lat 91, lat `true`, lat only, extra keys, control chars), bad emails, text file as `.jpg` → 415, PNG declared as JPEG → 415, truncated JPEG → 415, 5 MB+1 → 413, empty → 422, Spaces failure → 503 with no row, DB failure → 503 with object cleaned up, `completed` without report rejected by CHECK, missing/Basic auth → 401, wrong token and unknown reference → identical 404, token for another submission → 404, GET body has no email/`@`/image key/token hash, failed status returns only the sanitized message, logs at INFO contain no email/token/idempotency key/image key.

### Live submit with no worker running
```
$ curl -X POST localhost:8080/api/v1/submissions -H 'Idempotency-Key: …' -F image=@stop.jpg … 
{"status":"submitted","reference_id":"BUS-2026-74CJCC1C","submission_status":"pending",…,"receipt_token":"<redacted>"}
HTTP 202 in 0.029682s
```

### Same row in PostgreSQL (redacted)
```
 reference_id      | status  | observations                            | report_null | email | private_key_ok | hash_len
 BUS-2026-74CJCC1C | pending | ["trees_vegetation", "missing_seating"] | t           | de*** | t              | 64

 has_job | attempts | done_at
 t       | 0        |
```

### Status endpoint
- With token → 200, `status: "pending"`, `report: null`.
- Wrong token → 404 `NOT_FOUND`.

### UI (headless Chrome, scripted)
- 320px viewport: no horizontal scroll.
- Empty submit: error summary shown and focused, lists 4 problems, fields get `aria-invalid="true"`.
- Keyboard-only: checkboxes toggled with Space, submit with Enter → exactly **1** POST, carrying an `Idempotency-Key`.
- After 202: focus moves to "Submitted successfully"; reference and "Pending: waiting for analysis" shown; token not in URL.
- Reload: receipt and polling resume from sessionStorage.
- GPS mode: address input disabled, coordinate inputs shown.
- **FIXTURE report** written directly to the row (labeled "FIXTURE (not a model output)") with `<img onerror>` in the description: rendered as literal text, 0 injected elements, no script ran; "Contact not verified." shown; focus moved to report heading.

### Worker (STAND-IN model, real PostgreSQL)
Covered: completed report saved on the **same row** (same `id`), original observations untouched, server-sourced fields from the stored record, prompt contains no email; fenced JSON accepted; invalid JSON retried then `failed` with `INVALID_MODEL_OUTPUT` after 3 attempts and no report; transient error → retry after backoff → `completed`; a replayed job cannot overwrite a completed report or create a new row; crash after claim → lease expires → completed once; crash on final attempt → swept to `failed`; missing image → `IMAGE_UNAVAILABLE` retry; 8 schema-violation cases rejected.

### Not yet performed
- Real Gemma 4 call (blocked as above) and acceptance check 3 (bench outside frame) with actual model output.
- Screen reader (VoiceOver/NVDA) pass — manual.
- Real browser offline/retry test — manual.
- Anything on DigitalOcean.

## Known issues / decisions
- Receipt tokens for requests with an `Idempotency-Key` are derived (HMAC of reference + key) so a retried POST returns the same token.
- Wrong token returns 404 (not 403) to avoid reference enumeration.
- EXIF metadata is kept in the private object (no re-encoding).
- No rate limiting.
