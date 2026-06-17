"""Public installer download endpoint.

URL format:
    GET /api/v1/downloads/{enrollment_token}/TimeTracker.dmg
    GET /api/v1/downloads/{enrollment_token}/TimeTracker.msi

The enrollment token scopes the request to the correct company.
Each hit generates a fresh 15-minute S3 pre-signed URL and issues a 302
redirect — the browser follows it and downloads the file directly from S3.
No AWS credentials are ever exposed in any URL visible to the downloader.
"""
import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.v1.endpoints.screenshots import _s3_client
from app.core.config import get_settings
from app.db.models import Company, InstallerVersion

log      = logging.getLogger(__name__)
router   = APIRouter()
settings = get_settings()

_PRESIGN_EXPIRY = 900  # 15 minutes

_EXT_TO_PLATFORM = {
    "dmg": "macos",
    "msi": "windows",
}

_ALLOWED_NAMES = {"TimeTracker.dmg", "TimeTracker.msi"}


@router.get("/{enrollment_token}/{filename}", include_in_schema=False)
async def public_download(
    enrollment_token: str,
    filename: str,
    db: AsyncSession = Depends(get_db),
):
    """Resolve enrollment token → company → active installer → 15-min pre-signed URL."""

    if filename not in _ALLOWED_NAMES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    ext = filename.rsplit(".", 1)[-1].lower()
    platform = _EXT_TO_PLATFORM[ext]

    # Resolve company from enrollment token.
    company = (await db.execute(
        select(Company).where(Company.enrollment_token == enrollment_token)
    )).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    # Find the active installer for this platform.
    installer = (await db.execute(
        select(InstallerVersion).where(
            InstallerVersion.company_id == company.id,
            InstallerVersion.platform   == platform,
            InstallerVersion.is_active  == True,  # noqa: E712
        )
    )).scalar_one_or_none()
    if not installer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active {platform} installer found",
        )

    # Generate a fresh 15-minute pre-signed S3 URL (no credentials in the URL).
    try:
        url = await asyncio.to_thread(
            _s3_client(public=True).generate_presigned_url,
            "get_object",
            Params={"Bucket": settings.S3_BUCKET, "Key": installer.s3_key},
            ExpiresIn=_PRESIGN_EXPIRY,
        )
    except Exception as e:
        log.error("Pre-signed URL generation failed (key=%s): %s", installer.s3_key, e)
        raise HTTPException(status_code=500, detail="Could not generate download link")

    log.info(
        "Public download: company=%s platform=%s version=%s",
        company.name, platform, installer.version,
    )
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)
