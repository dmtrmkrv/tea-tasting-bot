"""S3 storage implementation."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlparse
from uuid import uuid4

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.config import S3Settings
from app.storage.base import SavePhotoResult, Storage
from app.storage.local import _guess_extension

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class S3Health:
    ok: bool
    message: str


class S3Storage(Storage):
    backend: Final[str] = "s3"

    def __init__(self, settings: S3Settings) -> None:
        self.settings = settings
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.endpoint_url,
            region_name=settings.region,
            aws_access_key_id=settings.access_key,
            aws_secret_access_key=settings.secret_key,
        )

    async def save_photo(
        self,
        user_id: int,
        tasting_id: int,
        data: bytes,
        content_type: str,
    ) -> SavePhotoResult:
        extension = _guess_extension(content_type)
        key = f"users/{user_id}/tastings/{tasting_id}/{uuid4()}{extension}"
        try:
            self.client.put_object(
                Bucket=self.settings.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
        except (BotoCoreError, ClientError) as exc:
            logger.exception("Failed to upload photo to S3: %s", exc)
            raise

        return SavePhotoResult(
            backend="s3",
            key=key,
            url=_build_public_url(self.settings.endpoint_url, self.settings.bucket, key),
        )

    def health(self) -> S3Health:
        try:
            self.client.list_objects_v2(Bucket=self.settings.bucket, MaxKeys=1)
        except (BotoCoreError, ClientError) as exc:
            return S3Health(ok=False, message=str(exc))
        return S3Health(ok=True, message="OK")


def _build_public_url(endpoint: str, bucket: str, key: str) -> str:
    parsed = urlparse(endpoint)
    scheme = parsed.scheme or "https"
    host = parsed.netloc or parsed.path
    host = host.rstrip("/")
    return f"{scheme}://{host}/{bucket}/{key}"
