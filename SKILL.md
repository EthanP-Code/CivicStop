---
name: stopshade-api-ui
description: Low-level build spec for the StopShade submission API (FastAPI + PostgreSQL + DigitalOcean Spaces) and its single-page accessible UI. Use when implementing, reviewing, or testing the form, POST/GET submission endpoints, validation, receipt tokens, idempotency, or the jobs outbox.
---

# StopShade — API + UI build spec

Source of truth for product scope: `BUILD_BRIEF.md` v2.0. This file narrows it to
implementation detail for the **API** and **UI**. The worker is out of scope here
except for the shared `jobs` table contract (§4.3).

Golden rule: **`202` means the image is in Spaces AND the submission row + job row
are committed. Never wait for Gemma. Never return `submitted` on partial failure.**

---

## 1. Stack and layout

| Concern | Choice |
|---|---|
| Runtime | Python 3.12 |
| HTTP | FastAPI + Uvicorn |
| Multipart | `python-multipart` |
| Validation | Pydantic v2, `email-validator` |
| Image sniffing | magic bytes + Pillow `verify()` |
| DB | PostgreSQL 15+, `psycopg[binary,pool]` v3, raw SQL (no ORM) |
| Object storage | DigitalOcean Spaces via `boto3` (S3 API) |
| UI | Static `index.html` + `app.js` + `app.css`, served by FastAPI. No framework, no build step |

```
stopshade/
  app/
    main.py          # FastAPI app, routes, static mount, security headers
    config.py        # env loading, constants
    db.py            # connection pool, SQL functions
    storage.py       # Spaces put/delete
    validation.py    # image, location, email, observations
    tokens.py        # reference ID, receipt token, hashing
    errors.py        # ApiError + exception handlers
    worker.py        # (separate spec)
    static/
      index.html
      app.js
      app.css
  migrations/001_init.sql
  tests/
  Dockerfile
  requirements.txt
  README.md
  LICENSE
```

Run commands:
- API: `uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}`
- Worker: `python -m app.worker`

---

## 2. Configuration (`app/config.py`)

| Env var | Required | Notes |
|---|---|---|
| `DATABASE_URL` | yes | `postgresql://…?sslmode=require` on DO |
| `SPACES_BUCKET` | yes | private bucket |
| `SPACES_REGION` | yes | e.g. `nyc3` |
| `SPACES_ENDPOINT` | yes | `https://<region>.digitaloceanspaces.com` |
| `SPACES_ACCESS_KEY` | yes | |
| `SPACES_SECRET_KEY` | yes | |
| `TOKEN_HASH_SECRET` | yes | ≥32 random bytes, base64; used for HMAC |
| `MAX_IMAGE_BYTES` | no | default `5242880` (5 MB) |
| `GEMINI_API_KEY` | worker only | API must not need it |

Fail fast at startup if any required var is missing (log the **name**, never the value).

Constants:
```python
OBSERVATION_CATEGORIES = (
    "trees_vegetation", "missing_shelter", "missing_seating",
    "trash_cans", "bus_info_posters", "signage",
)
ADDRESS_MAX = 256
EMAIL_MAX = 254
ALLOWED_IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png"}
```

---

## 3. Database (`migrations/001_init.sql`)

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid() (built in on PG13+, harmless)

CREATE TYPE submission_status AS ENUM ('pending', 'processing', 'completed', 'failed');

CREATE TABLE submissions (
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
    image_content_type    TEXT NOT NULL CHECK (image_content_type IN ('image/jpeg','image/png')),
    report                JSONB,
    status                submission_status NOT NULL DEFAULT 'pending',
    error_metadata        JSONB,
    receipt_token_hash    TEXT NOT NULL,
    idempotency_key_hash  TEXT UNIQUE,
    CONSTRAINT report_iff_completed CHECK ((status = 'completed') = (report IS NOT NULL))
);

CREATE TABLE jobs (
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

CREATE INDEX jobs_ready_idx ON jobs (next_run_at) WHERE done_at IS NULL;
```

Notes:
- `observations` is written once by the API and **never updated** by anyone.
- `report_iff_completed` makes "completed without a report" impossible at the DB level.
- `jobs.submission_id UNIQUE` means one job per submission, so a replayed insert fails loudly.
- `updated_at` is set explicitly in every `UPDATE` (`updated_at = now()`); no trigger.

---

## 4. API

### 4.1 `POST /api/v1/submissions`

**Request:** `multipart/form-data`

| Part | Type | Rule |
|---|---|---|
| `image` | file | required; see image validation |
| `location` | text (JSON) | required; see location validation |
| `email` | text | required |
| `observations` | text (JSON array) | required |
| Header `Idempotency-Key` | string | optional; 8–128 chars, `[A-Za-z0-9_-]` |

**Processing order (exact):**

1. Validate `Idempotency-Key` format if present → else `422 INVALID_INPUT field=idempotency_key`.
2. If key present: `SELECT … WHERE idempotency_key_hash = $1`. If a row exists → go to **replay response** (step 9). No upload.
3. Validate `email`, `location`, `observations` (cheap checks first).
4. Read `image` in 64 KB chunks; abort at `MAX_IMAGE_BYTES + 1` → `413 IMAGE_TOO_LARGE`. Empty → `422`.
5. Validate image bytes (magic bytes → Pillow). Content type comes from **sniffed bytes**, not the client header. Mismatch with the declared header → `415 UNSUPPORTED_MEDIA`.
6. Generate `reference_id`, `receipt_token`, `image_key`.
7. `put_object` to Spaces (private). Failure → `503 STORAGE_UNAVAILABLE`, nothing written to DB.
8. One DB transaction:
   ```sql
   INSERT INTO submissions (reference_id, email, location, observations, image_key,
                            image_content_type, receipt_token_hash, idempotency_key_hash)
   VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
   ON CONFLICT (idempotency_key_hash) DO NOTHING
   RETURNING id, created_at;
   -- if a row returned:
   INSERT INTO jobs (submission_id) VALUES ($id);
   COMMIT;
   ```
   - No row returned (concurrent duplicate won the race) → delete the just-uploaded object, then go to the replay response.
   - Any DB error → rollback, best-effort `delete_object(image_key)`, `503 SERVICE_UNAVAILABLE`.
   - `reference_id` unique collision (astronomically unlikely) → regenerate and retry once.
9. Respond `202`.

**`202` body:**
```json
{
  "status": "submitted",
  "reference_id": "BUS-2026-7Q3MP82Z",
  "submission_status": "pending",
  "message": "Your observation has been submitted successfully.",
  "receipt_token": "<43-char urlsafe string>"
}
```
Replay response is identical in shape, with `submission_status` set to the row's current status.

Response headers: `Cache-Control: no-store`.

### 4.2 `GET /api/v1/submissions/{reference_id}`

- Header `Authorization: Bearer <receipt_token>` required. Missing or malformed → `401 UNAUTHORIZED`.
- Path must match the reference regex, else `404`.
- Lookup: `SELECT … WHERE reference_id = $1`, then `hmac.compare_digest(stored_hash, hash(token))`.
- Unknown reference **or** wrong token → **`404 NOT_FOUND`** with the same body for both, to prevent reference enumeration.
- Headers: `Cache-Control: no-store`.

**`200` body:**
```json
{
  "reference_id": "BUS-2026-7Q3MP82Z",
  "created_at": "2026-10-31T16:00:00Z",
  "updated_at": "2026-10-31T16:00:09Z",
  "status": "completed",
  "location": { "type": "address", "address": "Hillsborough St & Oberlin Rd" },
  "user_observations": ["trees_vegetation", "missing_seating"],
  "report": { "...": "report v1, only when status=completed, else null" },
  "error": null
}
```
- `status=failed` → `"error": {"code": "<sanitized>", "message": "<plain language>"}`, built from `error_metadata.code` via a fixed server-side lookup table. Never pass provider text through.
- **Never returned:** `email`, `image_key`, `receipt_token_hash`, `idempotency_key_hash`, `error_metadata` raw, internal `id`.

### 4.3 Jobs contract (shared with the worker)

The API only inserts into `jobs`. For reference, the worker claims with:
```sql
UPDATE jobs SET locked_by = $worker_id,
                locked_until = now() + interval '2 minutes',
                attempts = attempts + 1
WHERE id = (
    SELECT j.id FROM jobs j
    JOIN submissions s ON s.id = j.submission_id
    WHERE j.done_at IS NULL
      AND j.next_run_at <= now()
      AND (j.locked_until IS NULL OR j.locked_until < now())
      AND j.attempts < j.max_attempts
      AND s.status IN ('pending','processing')
    ORDER BY j.next_run_at
    FOR UPDATE OF j SKIP LOCKED
    LIMIT 1
)
RETURNING id, submission_id, attempts;
```
The final report write must include `WHERE id = $sid AND status <> 'completed'` so a duplicate job can never overwrite a completed report.

### 4.4 Other routes

| Route | Behavior |
|---|---|
| `GET /` | serves `static/index.html` |
| `GET /static/*` | static assets |
| `GET /healthz` | `SELECT 1`; `200 {"ok":true}` or `503`. No secrets, no counts |
| anything else | `404` JSON |

No list or index endpoint. No CORS middleware (same origin only).

### 4.5 Error envelope (`app/errors.py`)

```json
{ "error": { "code": "INVALID_INPUT", "message": "Choose at least one observation.", "field": "observations" } }
```

| HTTP | code | When |
|---|---|---|
| 401 | `UNAUTHORIZED` | missing or malformed bearer |
| 404 | `NOT_FOUND` | unknown ref, wrong token, unknown route |
| 413 | `IMAGE_TOO_LARGE` | > `MAX_IMAGE_BYTES` |
| 415 | `UNSUPPORTED_MEDIA` | not real JPEG/PNG, or header/bytes mismatch |
| 422 | `INVALID_INPUT` | any field rule; `field` set |
| 503 | `STORAGE_UNAVAILABLE` | Spaces put failed |
| 503 | `SERVICE_UNAVAILABLE` | DB/queue failure |
| 500 | `INTERNAL_ERROR` | unexpected; generic message, details only in server log |

Override FastAPI's default `RequestValidationError` handler so every 422 uses this envelope.

---

## 5. Validation details (`app/validation.py`)

**Email**
- `strip()`; length 3–254; `email_validator.validate_email(v, check_deliverability=False)`.
- Store the normalized form the library returns. No DNS lookups.

**Location** (the `location` part is a JSON string)
- `json.loads`; must be an object with `type` in `{"address","gps"}` and **no other keys** beyond those listed below.
- Address: `{"type":"address","address": str}`; strip; 1–256 chars; reject control characters (`\x00-\x1f`, `\x7f`).
- GPS: `{"type":"gps","latitude": num,"longitude": num}`; both required; reject bool, NaN, or Inf; lat −90..90, lng −180..180; round to 6 decimals on store.
- Store exactly the validated object as JSONB.

**Observations** (JSON string)
- `json.loads` → list of strings, length 1–6.
- Every item in `OBSERVATION_CATEGORIES`.
- **Duplicates → 422** (brief says "distinct values"; do not silently dedupe).
- Store in the order submitted.

**Image**
- Magic bytes: JPEG `FF D8 FF`; PNG `89 50 4E 47 0D 0A 1A 0A`.
- Pillow: `Image.open(BytesIO(b)).verify()`; `img.format` must be `JPEG` or `PNG` and match the magic bytes. Set `Image.MAX_IMAGE_PIXELS = 40_000_000` to block decompression bombs.
- Client filename is **ignored entirely** (never stored, never used in the key).
- No re-encoding in MVP. EXIF (which may contain GPS) stays in the private object; document this in the README privacy section.

---

## 6. Tokens and IDs (`app/tokens.py`)

```python
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32, no I L O U

def new_reference(now: datetime) -> str:
    return f"BUS-{now.year}-" + "".join(secrets.choice(ALPHABET) for _ in range(8))

def hmac_hex(purpose: str, value: str) -> str:
    return hmac.new(SECRET, f"{purpose}:{value}".encode(), "sha256").hexdigest()

def new_receipt_token(reference_id: str, idem_key: str | None) -> str:
    if idem_key is None:
        return secrets.token_urlsafe(32)
    # deterministic so a retried POST can return the same token
    raw = hmac.new(SECRET, f"receipt:{reference_id}:{idem_key}".encode(), "sha256").digest()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
```
- `receipt_token_hash = hmac_hex("rt", token)`
- `idempotency_key_hash = hmac_hex("idem", key)`
- Replay with the same idempotency key recomputes the token from `(reference_id, key)`. This is safe because only the original client knows the key (a `crypto.randomUUID()` that never leaves that tab).
- `image_key = f"submissions/{yyyy}/{mm}/{uuid4()}.{ext}"`. Not derived from the reference, email, or filename.

---

## 7. Storage (`app/storage.py`)

```python
s3 = boto3.client("s3", region_name=SPACES_REGION, endpoint_url=SPACES_ENDPOINT,
                  aws_access_key_id=..., aws_secret_access_key=...)

def put_image(key: str, data: bytes, content_type: str) -> None:
    s3.put_object(Bucket=SPACES_BUCKET, Key=key, Body=data,
                  ContentType=content_type, ACL="private")

def delete_image(key: str) -> None:  # best effort, log and swallow errors
    ...
```
- Never generate presigned or public URLs in the API (the UI uses its local preview only).
- Set boto3 timeouts (connect 5s, read 15s) and 2 retries so a stuck Spaces call can't hang the request.

---

## 8. Cross-cutting API rules

- **Logging:** log `reference_id`, status codes, timings, and error codes. **Never log** email, tokens, idempotency keys, image bytes, image keys, `Authorization` headers, or `DATABASE_URL`.
- **Security headers** (middleware on every response):
  - `Content-Security-Policy: default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`
  - `X-Content-Type-Options: nosniff`
  - `Referrer-Policy: no-referrer`
- Hence **no inline JS or CSS** in `index.html`.
- Request body cap at the proxy/Uvicorn level slightly above 5 MB as a second line of defense.
- DB pool: min 1, max 5 (DO dev DB connection limits are small).
- Out of scope (note as limitations in README): rate limiting, CAPTCHA, retention/deletion automation.

---

## 9. UI (`static/index.html`, `app.js`, `app.css`)

### 9.1 Page structure

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>StopShade — Report a bus stop</title>
  <link rel="stylesheet" href="/static/app.css">
  <script src="/static/app.js" defer></script>
</head>
<body>
  <a class="skip-link" href="#main">Skip to form</a>
  <header><h1>StopShade</h1><p>Report conditions at a Raleigh bus stop.</p></header>
  <main id="main">
    <div id="error-summary" role="alert" tabindex="-1" hidden></div>
    <form id="report-form" novalidate> … </form>
    <section id="receipt" aria-labelledby="receipt-heading" hidden> … </section>
    <section id="report-view" aria-labelledby="report-heading" hidden> … </section>
  </main>
  <footer><p>AI-assisted observation tool. Not an official city reporting channel.</p></footer>
</body>
</html>
```

### 9.2 Form fields (exact IDs)

| Field | Markup | Notes |
|---|---|---|
| Photo | `<input type="file" id="image" name="image" accept="image/jpeg,image/png" required aria-describedby="image-hint image-error">` | hint: "JPEG or PNG, up to 5 MB. Avoid photos where people's faces are identifiable." |
| Preview | `<img id="image-preview" alt="Preview of your selected photo" hidden>` | `URL.createObjectURL`; revoke the old URL on change |
| Location mode | `<fieldset><legend>Location</legend>` with radios `loc-mode-address` (default) / `loc-mode-gps` | toggles which sub-group is visible **and** enabled |
| Address | `<input type="text" id="address" maxlength="256" autocomplete="street-address">` | label: "Bus stop address or intersection" |
| GPS | button `#use-gps` "Use my current location", plus `#latitude` / `#longitude` (`inputmode="decimal"`, editable) | geolocation is only called on click; `{enableHighAccuracy:true, timeout:10000}` |
| GPS status | `<p id="gps-status" aria-live="polite">` | "Location captured." / "Location permission was denied. Enter an address instead." |
| Email | `<input type="email" id="email" maxlength="254" autocomplete="email" required>` | hint: "Stored with your report. We won't send you email." |
| Observations | `<fieldset aria-describedby="obs-hint obs-error"><legend>What did you notice? (choose at least one)</legend>` with 6 checkboxes, `name="observations"`, value = enum | labels exactly per brief; hint: "These are your observations. The photo may not confirm them." |
| Submit | `<button type="submit" id="submit-btn">Submit report</button>` | |

Every input has a `<label for>`. Each field has a `<p id="<field>-error" class="field-error">` that is empty until an error occurs.

### 9.3 Client validation (mirrors the server; the server stays authoritative)

On submit, clear all errors, then check:
- Image present, type in allowed set, size ≤ 5 MB.
- Address mode: trimmed length 1–256. GPS mode: both numbers within range.
- Email matches `/^[^\s@]+@[^\s@]+\.[^\s@]+$/` and length ≤ 254.
- At least one checkbox selected.

When a check fails:
- Set `aria-invalid="true"` on the field and fill its `-error` text.
- Fill `#error-summary` with a heading "Please fix N problems" and links to each field (`href="#email"`, etc.).
- Unhide the summary and call `.focus()` on it.

### 9.4 Submit flow (`app.js`)

```js
let idemKey = null;      // one per intended submission
let inFlight = false;

async function onSubmit(e) {
  e.preventDefault();
  if (inFlight || !validate()) return;
  inFlight = true;
  idemKey ??= crypto.randomUUID();
  setBusy(true);                         // disables button, text "Submitting…", aria-busy on form

  const fd = new FormData();
  fd.append("image", imageInput.files[0]);
  fd.append("location", JSON.stringify(buildLocation()));
  fd.append("email", emailInput.value.trim());
  fd.append("observations", JSON.stringify(checkedValues()));  // DOM order

  try {
    const res = await fetchWithRetry("/api/v1/submissions",
      { method: "POST", body: fd, headers: { "Idempotency-Key": idemKey } });
    const body = await res.json();
    if (res.status === 202) { onAccepted(body); idemKey = null; }
    else showServerError(res.status, body);   // map error.field → inline error
  } catch {
    showSummary("We couldn't reach the server. Your report may not have been saved. Try again; it won't create a duplicate.");
  } finally { inFlight = false; setBusy(false); }
}
```
- `fetchWithRetry`: retry **only on network errors** (not on any HTTP response), max 2 retries, 1s then 2s backoff, **same body and same idempotency key**.
- `idemKey` is reset only after a 202, or when the user changes the photo (a new photo means a new intended submission).

### 9.5 Receipt panel (on 202)

```
<h2 id="receipt-heading" tabindex="-1">Submitted successfully</h2>
Reference: <code id="ref-id">BUS-2026-…</code>  [Copy reference]
Status: <span id="status-text">Pending</span>
<p id="status-live" aria-live="polite"></p>
<p>Keep this tab open to see your report. Closing this browser tab ends access to it.</p>
```
- Hide the form, unhide the receipt, and call `.focus()` on `#receipt-heading`.
- Save `{reference_id, receipt_token}` to `sessionStorage["stopshade.receipt"]`, wrapped in try/catch. Never put the token in the URL or `localStorage`.
- On page load, if that key exists, show the receipt and resume polling.
- A "Submit another report" button clears sessionStorage, resets the form, and moves focus to the first field.

### 9.6 Polling

- `GET /api/v1/submissions/{ref}` with the bearer token, every **4 s**.
- Pause while `document.hidden`; resume on `visibilitychange`.
- Stop on `completed` or `failed`. Also stop after 5 minutes with: "Still processing. Check back in this tab later." plus a "Check again" button.
- `404` → "This report can't be found in this session." Clear sessionStorage.
- Network error → keep polling with backoff up to 15 s. Don't show an error unless 3 failures happen in a row.
- Update `#status-live` **only when the status changes** so screen readers aren't spammed.

Status labels (text plus icon, never color alone):

| status | Visible text |
|---|---|
| pending | "Pending: waiting for analysis" |
| processing | "Processing: analyzing your photo" |
| completed | "Completed: report ready" |
| failed | "Failed: " + server `error.message` |

### 9.7 Report view (on completed)

Render with **`textContent` / `createElement` only. Never `innerHTML`**, because model output is untrusted.

Sections in order:
1. Notice banner: `report.report_notice`.
2. Header: reference, submitted time (formatted in the user's locale), location.
3. "What you reported": user observations with friendly labels.
4. "What the photo shows": `image_description`, then `visual_findings` as a list. Map visibility to text: visible → "Seen in photo", not_visible → "Not seen in this photo", unclear → "Unclear", not_assessable → "Can't be assessed from this photo".
5. "Issues to review": each with an evidence badge (visual → "From photo", user_report → "Reported by you", both → "Photo + your report") and "Needs human review" when true.
6. "Suggested next steps": `recommended_actions` as `<ol>`.
7. "Who to contact": if `responsible_organization.name` is null → "Contact not verified." Otherwise show the name and contact, plus "Source" linking `source_url` (`rel="noopener noreferrer"`, only if it starts with `https://`), and "verified on {date}".
8. Footer: "Generated with {report.model}".

Move focus to `#report-heading` when the report renders.

### 9.8 CSS / accessibility requirements (`app.css`)

- Single column, `max-width: 40rem`, 16px side padding, no horizontal scroll at 320px.
- Base font 1rem (16px) or larger, line-height 1.5. Text contrast ≥ 4.5:1, UI borders ≥ 3:1.
- Touch targets ≥ 44×44px; checkboxes enlarged with a large clickable label area.
- `:focus-visible { outline: 3px solid <accent>; outline-offset: 2px; }`. Never `outline: none` without a replacement.
- Errors use red text **plus** an icon/prefix "Error:".
- `.skip-link` is visible on focus.
- `@media (prefers-reduced-motion: reduce)` disables transitions.
- `@media (prefers-color-scheme: dark)` support is optional; if added, re-check contrast.
- Hidden sections use the `hidden` attribute so they're also hidden from assistive tech.

### 9.9 Exact user-facing strings

| Key | Text |
|---|---|
| success heading | `Submitted successfully` |
| busy button | `Submitting…` |
| no-observations error | `Choose at least one observation.` |
| image type error | `Choose a JPEG or PNG photo.` |
| image size error | `This photo is larger than 5 MB. Choose a smaller one.` |
| address error | `Enter the bus stop address or nearest intersection.` |
| gps error | `Enter a valid latitude and longitude, or use an address.` |
| email error | `Enter a valid email address, like name@example.com.` |
| contact null | `Contact not verified.` |
| server 503 | `We couldn't save your report right now. Nothing was submitted. Please try again.` |

---

## 10. Verification checklist (API + UI)

Run each one and record the real output in `docs/ENGINEERING_REPORT.md`.

**API (pytest + httpx `TestClient` against a real local Postgres; Spaces via MinIO or a stubbed client, labeled as such)**
- [ ] Valid address + JPEG + 2 categories → 202. The row has `pending`, `report IS NULL`, observations unchanged, one `jobs` row.
- [ ] Valid GPS + PNG → 202.
- [ ] 202 latency stays under ~1s with the worker stopped (proves no inference on the request path).
- [ ] Same `Idempotency-Key` twice → same `reference_id` and same `receipt_token`, one row, one object in the bucket.
- [ ] Concurrent same-key POSTs → one row, and the orphan object is deleted.
- [ ] Missing each field → 422 with the correct `field`. Duplicate category → 422. Unknown category → 422. Empty array → 422.
- [ ] Address of 257 chars → 422. lat 91 → 422. lat as `true` → 422. Only `latitude` given → 422.
- [ ] `.txt` renamed to `.jpg` → 415. PNG bytes with `Content-Type: image/jpeg` → 415. 5 MB + 1 byte → 413. 0 bytes → 422.
- [ ] Spaces put fails → 503, no DB row.
- [ ] DB insert fails → 503, object deleted, no row.
- [ ] GET without a token → 401. Wrong token → 404. Unknown ref → 404 (same body as wrong token).
- [ ] GET response JSON never contains the substring `@` from the email, or `image_key`.
- [ ] Logs from the test run contain no email, token, or idempotency key (grep them).
- [ ] Manually setting a row to `completed` without a report → rejected by the CHECK constraint.

**UI (manual, recorded)**
- [ ] Full submit using only the keyboard (Tab / Space / Enter); focus is always visible.
- [ ] Error summary receives focus, links jump to the fields, and fields are announced as invalid.
- [ ] A screen reader (VoiceOver or NVDA) announces "Submitted successfully" and status changes once each.
- [ ] Double-clicking Submit produces one request (check the Network tab).
- [ ] With the network offline mid-submit, the retry reuses the same key and only one row is created.
- [ ] 320px-wide viewport: no horizontal scroll, all controls usable.
- [ ] GPS permission denied → helpful message, address still works.
- [ ] Reloading the tab after submit resumes the receipt and polling.
- [ ] Report with HTML in model text (fixture, labeled) renders as literal text.
- [ ] Null contact shows "Contact not verified."
