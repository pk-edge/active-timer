"""Single choke point for release-file URLs so we can swap presigned-S3 for a
CDN later by setting CDN_BASE_URL — no caller changes."""
from __future__ import annotations

from app.api.v1.endpoints.screenshots import _s3_client
from app.core.config import get_settings

settings = get_settings()


def _presign(s3_key: str) -> str:
    return _s3_client(public=True).generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.S3_BUCKET, "Key": s3_key},
        ExpiresIn=3600,
    )


def download_url(s3_key: str) -> str:
    base = (settings.CDN_BASE_URL or "").rstrip("/")
    if base:
        return f"{base}/{s3_key}"
    return _presign(s3_key)
