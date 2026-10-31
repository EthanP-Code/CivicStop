"""Test fixtures: real local PostgreSQL + moto-mocked S3 (stand-in for Spaces)."""
import base64
import io
import os

import boto3
import psycopg
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws
from PIL import Image

from app import db
from app.config import Settings
from app.main import create_app
from app.migrate import migrate
from app.storage import Storage

TEST_DB = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://stopshade:localdev@localhost:5432/stopshade_test"
)
BUCKET = "stopshade-test"


@pytest.fixture(scope="session")
def settings():
    return Settings(
        database_url=TEST_DB,
        spaces_bucket=BUCKET,
        spaces_region="us-east-1",
        spaces_endpoint="https://example.invalid",
        spaces_access_key="test",
        spaces_secret_key="test",
        token_hash_secret=base64.b64decode(base64.b64encode(b"x" * 32)),
        max_image_bytes=5 * 1024 * 1024,
    )


@pytest.fixture(scope="session")
def pool(settings):
    with psycopg.connect(TEST_DB, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS jobs, submissions, schema_migrations CASCADE")
        conn.execute("DROP TYPE IF EXISTS submission_status")
    migrate(TEST_DB)
    p = db.make_pool(TEST_DB)
    yield p
    p.close()


@pytest.fixture(autouse=True)
def clean(request):
    if request.node.get_closest_marker("no_db"):
        yield
        return
    pool = request.getfixturevalue("pool")
    with pool.connection() as conn:
        conn.execute("TRUNCATE jobs, submissions")
    yield


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture
def storage(settings, s3):
    return Storage(settings, client=s3)


@pytest.fixture
def client(settings, pool, storage):
    app = create_app(settings=settings, pool=pool, storage=storage)
    with TestClient(app) as c:
        yield c


def make_image(fmt="JPEG", size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (90, 140, 100)).save(buf, format=fmt)
    return buf.getvalue()


@pytest.fixture
def jpeg():
    return make_image("JPEG")


@pytest.fixture
def png():
    return make_image("PNG")


def bucket_keys(s3) -> list[str]:
    return [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])]
