"""Private object storage over the S3 API (MinIO in development).

Objects are never served by the API. Downloads use short-lived presigned URLs signed
for the public endpoint, always as attachments, so a stored file can't run as a page.
"""

from __future__ import annotations

import re
from typing import Any

import aioboto3
from aiobotocore.config import AioConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings, get_settings
from app.core.errors import AppError


class StorageUnavailable(AppError):
    status_code = 503
    code = "storage_unavailable"
    message = "File storage is not available right now. Please try again shortly."


def storage_configured(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(settings.s3_endpoint_url and settings.s3_access_key_id and settings.s3_secret_access_key)


def _client(endpoint: str | None = None) -> Any:
    settings = get_settings()
    if not storage_configured(settings):
        raise StorageUnavailable("File storage is not configured (S3_* settings).")
    assert settings.s3_access_key_id and settings.s3_secret_access_key
    session = aioboto3.Session()
    return session.client(
        "s3",
        endpoint_url=endpoint or settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key_id.get_secret_value(),
        aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
        config=AioConfig(
            signature_version="s3v4", s3={"addressing_style": "path"}, retries={"max_attempts": 3}
        ),
    )


async def ensure_bucket() -> bool:
    """Create the private bucket if it doesn't exist. Returns True if it was created."""
    bucket = get_settings().s3_bucket
    try:
        async with _client() as s3:
            try:
                await s3.head_bucket(Bucket=bucket)
                return False
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") not in ("404", "NoSuchBucket", "NotFound"):
                    raise
            await s3.create_bucket(Bucket=bucket)
            return True
    except (BotoCoreError, ClientError) as exc:
        raise StorageUnavailable(f"Could not prepare the bucket: {exc}") from exc


async def put(key: str, body: bytes, content_type: str) -> None:
    try:
        async with _client() as s3:
            await s3.put_object(Bucket=get_settings().s3_bucket, Key=key, Body=body, ContentType=content_type)
    except (BotoCoreError, ClientError) as exc:
        raise StorageUnavailable() from exc


async def get(key: str, max_bytes: int) -> bytes:
    try:
        async with _client() as s3:
            obj = await s3.get_object(Bucket=get_settings().s3_bucket, Key=key)
            if int(obj.get("ContentLength") or 0) > max_bytes:
                raise StorageUnavailable("The stored file is larger than allowed.")
            async with obj["Body"] as stream:
                data: bytes = await stream.read()
            return data
    except (BotoCoreError, ClientError) as exc:
        raise StorageUnavailable() from exc


async def delete(key: str) -> None:
    try:
        async with _client() as s3:
            await s3.delete_object(Bucket=get_settings().s3_bucket, Key=key)
    except (BotoCoreError, ClientError) as exc:
        raise StorageUnavailable() from exc


_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_filename(name: str) -> str:
    cleaned = _UNSAFE.sub("_", name).strip(" .") or "download"
    return cleaned[:150]


async def download_url(key: str, filename: str) -> str:
    settings = get_settings()
    disposition = f'attachment; filename="{safe_filename(filename)}"'
    async with _client(settings.s3_public_endpoint_url or settings.s3_endpoint_url) as s3:
        url: str = await s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": settings.s3_bucket,
                "Key": key,
                "ResponseContentDisposition": disposition,
                "ResponseContentType": "application/octet-stream",
            },
            ExpiresIn=settings.s3_presign_seconds,
        )
    return url
