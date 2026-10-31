import io
import json
import logging
import threading
import time

import psycopg
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import create_app
from tests.conftest import BUCKET, TEST_DB, bucket_keys

EMAIL = "demo.rider@example.com"
ADDRESS = {"type": "address", "address": "Hillsborough St & Oberlin Rd"}
OBS = ["trees_vegetation", "missing_seating"]


def post(client, image, *, location=ADDRESS, email=EMAIL, observations=OBS, ctype="image/jpeg",
         key=None, drop=None, raw=None):
    data = {"location": json.dumps(location), "email": email, "observations": json.dumps(observations)}
    if raw:
        data.update(raw)
    files = {"image": ("photo.jpg", image, ctype)}
    if drop == "image":
        files = None
    elif drop:
        data.pop(drop)
    headers = {"Idempotency-Key": key} if key else {}
    return client.post("/api/v1/submissions", data=data, files=files, headers=headers)


def rows(pool):
    with pool.connection() as conn:
        return conn.execute("SELECT *, status::text AS status FROM submissions").fetchall()


def job_count(pool):
    with pool.connection() as conn:
        return conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"]


# ---------- happy path ----------

def test_happy_path_address_jpeg(client, pool, s3, jpeg):
    started = time.perf_counter()
    r = post(client, jpeg)
    elapsed = time.perf_counter() - started
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "submitted"
    assert body["submission_status"] == "pending"
    assert body["reference_id"].startswith("BUS-")
    assert len(body["receipt_token"]) >= 40
    assert r.headers["cache-control"] == "no-store"
    assert elapsed < 1.0, f"202 took {elapsed:.2f}s"

    [row] = rows(pool)
    assert row["status"] == "pending"
    assert row["report"] is None
    assert row["observations"] == OBS
    assert row["location"] == ADDRESS
    assert row["email"] == EMAIL
    assert row["receipt_token_hash"] != body["receipt_token"]
    assert job_count(pool) == 1
    keys = bucket_keys(s3)
    assert keys == [row["image_key"]]
    assert "photo" not in row["image_key"]
    head = s3.head_object(Bucket=BUCKET, Key=row["image_key"])
    assert head["ContentType"] == "image/jpeg"


def test_happy_path_gps_png(client, pool, png):
    r = post(client, png, location={"type": "gps", "latitude": 35.7796123456, "longitude": -78.6382}, ctype="image/png")
    assert r.status_code == 202, r.text
    [row] = rows(pool)
    assert row["location"] == {"type": "gps", "latitude": 35.779612, "longitude": -78.6382}
    assert row["image_content_type"] == "image/png"


def test_reference_ids_unique(client, jpeg):
    refs = {post(client, jpeg).json()["reference_id"] for _ in range(5)}
    assert len(refs) == 5


# ---------- idempotency ----------

def test_idempotent_replay(client, pool, s3, jpeg):
    key = "k-0123456789abcdef"
    a = post(client, jpeg, key=key).json()
    b = post(client, jpeg, key=key).json()
    assert a["reference_id"] == b["reference_id"]
    assert a["receipt_token"] == b["receipt_token"]
    assert len(rows(pool)) == 1
    assert job_count(pool) == 1
    assert len(bucket_keys(s3)) == 1
    # replayed token still authorizes reads
    g = client.get(f"/api/v1/submissions/{b['reference_id']}", headers={"Authorization": f"Bearer {b['receipt_token']}"})
    assert g.status_code == 200


def test_concurrent_same_key(settings, pool, storage, s3, jpeg):
    app = create_app(settings=settings, pool=pool, storage=storage)
    key = "concurrent-key-123456"
    results = []
    with TestClient(app) as c:
        def go():
            results.append(post(c, jpeg, key=key))
        threads = [threading.Thread(target=go) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
    assert all(r.status_code == 202 for r in results), [r.text for r in results]
    assert len({r.json()["reference_id"] for r in results}) == 1
    assert len(rows(pool)) == 1
    assert job_count(pool) == 1
    assert len(bucket_keys(s3)) == 1  # orphans cleaned up


def test_bad_idempotency_key(client, jpeg):
    r = post(client, jpeg, key="short")
    assert r.status_code == 422
    assert r.json()["error"]["field"] == "idempotency_key"


# ---------- invalid inputs ----------

@pytest.mark.parametrize("field", ["image", "location", "email", "observations"])
def test_missing_field(client, pool, jpeg, field):
    r = post(client, jpeg, drop=field)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "INVALID_INPUT"
    assert r.json()["error"]["field"] == field
    assert rows(pool) == []


@pytest.mark.parametrize("obs", [[], ["missing_seating", "missing_seating"], ["bench"], "signage", [1]])
def test_bad_observations(client, pool, jpeg, obs):
    r = post(client, jpeg, observations=obs)
    assert r.status_code == 422
    assert r.json()["error"]["field"] == "observations"
    assert rows(pool) == []


@pytest.mark.parametrize("loc", [
    {"type": "address", "address": "x" * 257},
    {"type": "address", "address": "   "},
    {"type": "address", "address": "Main St\x00"},
    {"type": "address", "address": "Main St", "extra": 1},
    {"type": "gps", "latitude": 91, "longitude": 0},
    {"type": "gps", "latitude": 0, "longitude": -181},
    {"type": "gps", "latitude": True, "longitude": 0},
    {"type": "gps", "latitude": 35.0},
    {"type": "map"},
    ["not", "an", "object"],
])
def test_bad_location(client, pool, jpeg, loc):
    r = post(client, jpeg, location=loc)
    assert r.status_code == 422, loc
    assert r.json()["error"]["field"] == "location"
    assert rows(pool) == []


def test_location_not_json(client, jpeg):
    r = post(client, jpeg, raw={"location": "{not json"})
    assert r.status_code == 422


@pytest.mark.parametrize("email", ["", "no-at-sign", "a@b", "x@" + "a" * 250 + ".com", "two@@example.com"])
def test_bad_email(client, pool, jpeg, email):
    r = post(client, jpeg, email=email)
    assert r.status_code == 422
    assert r.json()["error"]["field"] == "email"
    assert rows(pool) == []


def test_text_file_renamed_jpg(client, pool):
    r = post(client, b"hello, this is not an image")
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "UNSUPPORTED_MEDIA"
    assert rows(pool) == []


def test_png_declared_as_jpeg(client, pool, png):
    r = post(client, png, ctype="image/jpeg")
    assert r.status_code == 415
    assert rows(pool) == []


def test_multi_picture_jpeg_accepted(client, pool):
    buf = io.BytesIO()
    Image.new("RGB", (64, 48)).save(
        buf, "MPO", save_all=True, append_images=[Image.new("RGB", (64, 48))]
    )
    r = post(client, buf.getvalue())
    assert r.status_code == 202
    assert len(rows(pool)) == 1


def test_truncated_jpeg(client, jpeg):
    r = post(client, jpeg[:20])
    assert r.status_code == 415


def test_oversized(client, pool, s3, jpeg):
    big = jpeg + b"\x00" * (5 * 1024 * 1024 + 1 - len(jpeg))
    r = post(client, big)
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "IMAGE_TOO_LARGE"
    assert rows(pool) == []
    assert bucket_keys(s3) == []


def test_empty_image(client, pool):
    r = post(client, b"")
    assert r.status_code == 422
    assert rows(pool) == []


# ---------- failure paths ----------

class BrokenPutStorage:
    def put_image(self, *a):
        raise RuntimeError("spaces down")

    def delete_image(self, *a):
        pass


def test_storage_failure(settings, pool, jpeg):
    app = create_app(settings=settings, pool=pool, storage=BrokenPutStorage())
    with TestClient(app) as c:
        r = post(c, jpeg)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "STORAGE_UNAVAILABLE"
    assert rows(pool) == []


def test_db_failure_cleans_object(settings, pool, storage, s3, jpeg, monkeypatch):
    from app import db as dbmod

    def boom(*a, **k):
        raise psycopg.OperationalError("db down")

    monkeypatch.setattr(dbmod, "insert_submission", boom)
    app = create_app(settings=settings, pool=pool, storage=storage)
    with TestClient(app) as c:
        r = post(c, jpeg)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert r.json().get("status") != "submitted"
    assert rows(pool) == []
    assert bucket_keys(s3) == []


def test_completed_without_report_rejected(client, pool, jpeg):
    post(client, jpeg)
    with pytest.raises(psycopg.errors.CheckViolation):
        with pool.connection() as conn:
            conn.execute("UPDATE submissions SET status = 'completed'")


# ---------- status / security ----------

def test_get_status_security(client, pool, jpeg):
    body = post(client, jpeg).json()
    ref, token = body["reference_id"], body["receipt_token"]
    url = f"/api/v1/submissions/{ref}"

    assert client.get(url).status_code == 401
    assert client.get(url, headers={"Authorization": "Basic abc"}).status_code == 401

    wrong = client.get(url, headers={"Authorization": "Bearer " + "A" * 43})
    unknown = client.get("/api/v1/submissions/BUS-2026-ZZZZZZZZ", headers={"Authorization": f"Bearer {token}"})
    assert wrong.status_code == unknown.status_code == 404
    assert wrong.json() == unknown.json()

    ok = client.get(url, headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200
    data = ok.json()
    assert data["status"] == "pending"
    assert data["report"] is None and data["error"] is None
    assert data["user_observations"] == OBS
    assert set(data) == {"reference_id", "created_at", "updated_at", "status", "location", "user_observations", "report", "error"}
    text = ok.text
    assert "@" not in text
    [row] = rows(pool)
    assert row["image_key"] not in text
    assert row["receipt_token_hash"] not in text


def test_token_for_other_submission_rejected(client, jpeg):
    a = post(client, jpeg).json()
    b = post(client, jpeg).json()
    r = client.get(f"/api/v1/submissions/{a['reference_id']}", headers={"Authorization": f"Bearer {b['receipt_token']}"})
    assert r.status_code == 404


def test_failed_status_is_sanitized(client, pool, jpeg):
    body = post(client, jpeg).json()
    with pool.connection() as conn:
        conn.execute(
            "UPDATE submissions SET status='failed', error_metadata=%s",
            (json.dumps({"code": "MODEL_UNAVAILABLE", "detail": "provider said: secret internal trace"}),),
        )
    r = client.get(f"/api/v1/submissions/{body['reference_id']}", headers={"Authorization": f"Bearer {body['receipt_token']}"})
    assert r.json()["status"] == "failed"
    assert r.json()["error"]["code"] == "MODEL_UNAVAILABLE"
    assert "secret" not in r.text


def test_completed_returns_report(client, pool, jpeg):
    body = post(client, jpeg).json()
    report = {"schema_version": "1", "image_description": "fixture"}
    with pool.connection() as conn:
        conn.execute("UPDATE submissions SET status='completed', report=%s", (json.dumps(report),))
    r = client.get(f"/api/v1/submissions/{body['reference_id']}", headers={"Authorization": f"Bearer {body['receipt_token']}"})
    assert r.json()["report"] == report


def test_logs_have_no_secrets(client, jpeg, caplog):
    caplog.set_level(logging.INFO)  # production log level
    key = "log-check-key-0001"
    body = post(client, jpeg, key=key).json()
    client.get(f"/api/v1/submissions/{body['reference_id']}", headers={"Authorization": f"Bearer {body['receipt_token']}"})
    logs = caplog.text
    assert EMAIL not in logs
    assert body["receipt_token"] not in logs
    assert key not in logs
    assert "submissions/" + "20" not in logs  # no image keys


# ---------- other routes ----------

def test_index_and_headers(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Submit report" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/static/app.js").status_code == 200


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_unknown_route_404_json(client):
    r = client.get("/api/v1/submissions")
    assert r.status_code in (404, 405)
    assert "error" in r.json()
