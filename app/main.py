"""CivitStop API: accepts bus-stop observations and serves status/report."""
import hmac
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from . import db, tokens
from .config import ALLOWED_IMAGE_TYPES, Settings, load_settings
from .errors import NOT_FOUND, ApiError, error_response, install_handlers, invalid
from .storage import Storage
from .validation import (
    sniff_image,
    validate_email_field,
    validate_location,
    validate_observations,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("civitstop.api")

STATIC_DIR = Path(__file__).parent / "static"
CHUNK = 64 * 1024

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

# Safe, user-facing messages for failed submissions. Provider text never passes through.
FAILURE_MESSAGES = {
    "MODEL_UNAVAILABLE": "The analysis service was unavailable. Your observation is saved.",
    "INVALID_MODEL_OUTPUT": "The analysis couldn't produce a valid report. Your observation is saved.",
    "IMAGE_UNAVAILABLE": "Your photo couldn't be read for analysis. Your observation is saved.",
}
DEFAULT_FAILURE = "We couldn't generate a report for this observation. Your observation is saved."


def create_app(settings: Settings | None = None, pool=None, storage: Storage | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owned_pool = None
        if app.state.settings is None:
            app.state.settings = load_settings()
        if app.state.pool is None:
            owned_pool = db.make_pool(app.state.settings.database_url)
            app.state.pool = owned_pool
        if app.state.storage is None:
            app.state.storage = Storage(app.state.settings)
        yield
        if owned_pool is not None:
            owned_pool.close()

    app = FastAPI(title="CivitStop", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.pool = pool
    app.state.storage = storage
    install_handlers(app)

    @app.middleware("http")
    async def guard_and_headers(request: Request, call_next):
        started = time.perf_counter()
        cfg: Settings | None = request.app.state.settings
        length = request.headers.get("content-length")
        limit = (cfg.max_image_bytes if cfg else 5_242_880) + 512 * 1024
        if length and length.isdigit() and int(length) > limit:
            response = error_response(413, "IMAGE_TOO_LARGE", "This photo is larger than 5 MB. Choose a smaller one.", "image")
        else:
            response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        log.info(
            "%s %s -> %s in %.0fms",
            request.method,
            request.url.path,
            response.status_code,
            (time.perf_counter() - started) * 1000,
        )
        return response

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/healthz")
    def healthz(request: Request):
        try:
            db.ping(request.app.state.pool)
        except Exception:
            return JSONResponse({"ok": False}, status_code=503)
        return {"ok": True}

    @app.post("/api/v1/submissions")
    async def create_submission(request: Request):
        cfg: Settings = request.app.state.settings
        pool = request.app.state.pool
        store: Storage = request.app.state.storage

        # 1. Idempotency key format
        idem_key = request.headers.get("idempotency-key")
        if idem_key is not None and not tokens.IDEMPOTENCY_RE.match(idem_key):
            raise invalid("idempotency_key", "Invalid idempotency key.")
        idem_hash = tokens.idempotency_hash(cfg.token_hash_secret, idem_key) if idem_key else None

        try:
            form = await request.form(max_files=1, max_fields=8)
        except Exception:
            raise invalid("image", "The form couldn't be read. Please try again.")

        # 2. Replay of an earlier successful submission
        if idem_hash:
            existing = await _run_db(db.find_by_idempotency, pool, idem_hash)
            if existing:
                return _accepted(cfg, existing["reference_id"], existing["status"], idem_key)

        # 3. Cheap field checks
        email = validate_email_field(_text(form, "email", "Enter a valid email address, like name@example.com."))
        location = validate_location(_text(form, "location", "Enter the bus stop address or nearest intersection."))
        observations = validate_observations(_text(form, "observations", "Choose at least one observation."))

        # 4-5. Image size and content
        upload = form.get("image")
        if not isinstance(upload, UploadFile):
            raise invalid("image", "Choose a JPEG or PNG photo.")
        data = await _read_capped(upload, cfg.max_image_bytes)
        if not data:
            raise invalid("image", "Choose a JPEG or PNG photo.")
        content_type = sniff_image(data, upload.content_type)

        # 6. IDs
        now = datetime.now(timezone.utc)
        image_key = tokens.new_image_key(now, ALLOWED_IMAGE_TYPES[content_type])

        # 7. Private object storage
        try:
            await run_in_threadpool(store.put_image, image_key, data, content_type)
        except Exception as exc:
            log.error("image upload failed: %s", type(exc).__name__)
            raise ApiError(503, "STORAGE_UNAVAILABLE", "We couldn't save your report right now. Nothing was submitted. Please try again.")

        # 8. Submission + job in one transaction
        for attempt in range(2):
            reference_id = tokens.new_reference(now)
            receipt_token = tokens.new_receipt_token(cfg.token_hash_secret, reference_id, idem_key)
            record = db.NewSubmission(
                reference_id=reference_id,
                email=email,
                location=location,
                observations=observations,
                image_key=image_key,
                image_content_type=content_type,
                receipt_token_hash=tokens.receipt_hash(cfg.token_hash_secret, receipt_token),
                idempotency_key_hash=idem_hash,
            )
            try:
                row = await run_in_threadpool(db.insert_submission, pool, record)
                break
            except psycopg.errors.UniqueViolation as exc:
                if attempt == 0 and "reference_id" in str(exc.diag.constraint_name or ""):
                    continue
                await run_in_threadpool(store.delete_image, image_key)
                log.error("submission insert failed: UniqueViolation")
                raise ApiError(503, "SERVICE_UNAVAILABLE", "We couldn't save your report right now. Nothing was submitted. Please try again.")
            except Exception as exc:
                await run_in_threadpool(store.delete_image, image_key)
                log.error("submission insert failed: %s", type(exc).__name__)
                raise ApiError(503, "SERVICE_UNAVAILABLE", "We couldn't save your report right now. Nothing was submitted. Please try again.")

        if row is None:
            # A concurrent request with the same key committed first.
            await run_in_threadpool(store.delete_image, image_key)
            existing = await _run_db(db.find_by_idempotency, pool, idem_hash)
            if not existing:
                raise ApiError(503, "SERVICE_UNAVAILABLE", "We couldn't save your report right now. Please try again.")
            return _accepted(cfg, existing["reference_id"], existing["status"], idem_key)

        log.info("submission accepted reference=%s", row["reference_id"])
        return JSONResponse(
            {
                "status": "submitted",
                "reference_id": row["reference_id"],
                "submission_status": row["status"],
                "message": "Your observation has been submitted successfully.",
                "receipt_token": receipt_token,
            },
            status_code=202,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/submissions/{reference_id}")
    async def get_submission(reference_id: str, request: Request):
        cfg: Settings = request.app.state.settings
        auth = request.headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not 16 <= len(token) <= 128:
            raise ApiError(401, "UNAUTHORIZED", "A valid receipt token is required.")
        if not tokens.REFERENCE_RE.match(reference_id):
            raise ApiError(404, *NOT_FOUND)
        row = await _run_db(db.get_submission, request.app.state.pool, reference_id)
        expected = row["receipt_token_hash"] if row else "0" * 64
        supplied = tokens.receipt_hash(cfg.token_hash_secret, token)
        if not hmac.compare_digest(expected, supplied) or row is None:
            raise ApiError(404, *NOT_FOUND)

        error = None
        if row["status"] == "failed":
            code = (row["error_metadata"] or {}).get("code") or "PROCESSING_FAILED"
            code = code if code in FAILURE_MESSAGES else "PROCESSING_FAILED"
            error = {"code": code, "message": FAILURE_MESSAGES.get(code, DEFAULT_FAILURE)}

        return JSONResponse(
            {
                "reference_id": row["reference_id"],
                "created_at": db.iso(row["created_at"]),
                "updated_at": db.iso(row["updated_at"]),
                "status": row["status"],
                "location": row["location"],
                "user_observations": row["observations"],
                "report": row["report"] if row["status"] == "completed" else None,
                "error": error,
            },
            headers={"Cache-Control": "no-store"},
        )

    return app


def _text(form, name: str, message: str) -> str:
    value = form.get(name)
    if value is None or isinstance(value, UploadFile):
        raise invalid(name, message)
    return value


async def _read_capped(upload: UploadFile, max_bytes: int) -> bytes:
    buf = bytearray()
    while True:
        chunk = await upload.read(CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise ApiError(413, "IMAGE_TOO_LARGE", "This photo is larger than 5 MB. Choose a smaller one.", "image")
    return bytes(buf)


async def _run_db(fn, *args):
    try:
        return await run_in_threadpool(fn, *args)
    except ApiError:
        raise
    except Exception as exc:
        log.error("database error: %s", type(exc).__name__)
        raise ApiError(503, "SERVICE_UNAVAILABLE", "The service is temporarily unavailable. Please try again.")


def _accepted(cfg: Settings, reference_id: str, status: str, idem_key: str) -> JSONResponse:
    token = tokens.new_receipt_token(cfg.token_hash_secret, reference_id, idem_key)
    return JSONResponse(
        {
            "status": "submitted",
            "reference_id": reference_id,
            "submission_status": status,
            "message": "Your observation has been submitted successfully.",
            "receipt_token": token,
        },
        status_code=202,
        headers={"Cache-Control": "no-store"},
    )


app = create_app()
