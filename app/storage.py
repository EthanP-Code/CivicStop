"""Private object storage on DigitalOcean Spaces (S3 API)."""
import logging

import boto3
from botocore.config import Config

from .config import Settings

log = logging.getLogger("civitstop.storage")


class Storage:
    def __init__(self, settings: Settings, client=None):
        self.bucket = settings.spaces_bucket
        self.client = client or boto3.client(
            "s3",
            region_name=settings.spaces_region,
            endpoint_url=settings.spaces_endpoint,
            aws_access_key_id=settings.spaces_access_key,
            aws_secret_access_key=settings.spaces_secret_key,
            config=Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 2}),
        )

    def put_image(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(
            Bucket=self.bucket, Key=key, Body=data, ContentType=content_type, ACL="private"
        )

    def get_image(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def delete_image(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # best effort
            log.warning("orphan image cleanup failed: %s", type(exc).__name__)
