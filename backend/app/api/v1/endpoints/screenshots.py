import uuid
import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import List

import boto3
from botocore.config import Config as BotoConfig
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.deps import get_db, get_current_device, get_current_admin, require
from app.core.config import get_settings
from app.core.scoping import visible_device_ids, ALL
from app.db.models import Screenshot, Device, Employee
from app.schemas.screenshot import UploadRequest, UploadResponse, ScreenshotOut

log      = logging.getLogger(__name__)
router   = APIRouter()
settings = get_settings()


@lru_cache(maxsize=4)
def _s3_client(public: bool = False):
    """
    public=True  → uses S3_PUBLIC_ENDPOINT_URL for presigned URLs the agent will PUT to
    public=False → uses S3_ENDPOINT_URL for internal operations (list, delete, get)

    Cached: client construction costs ~10ms and boto3 clients are thread-safe,
    so building one per request (or per presigned URL) wastes CPU on hot paths.
    """
    kwargs = {"region_name": settings.AWS_REGION}
    if settings.AWS_ACCESS_KEY_ID:
        kwargs["aws_access_key_id"]     = settings.AWS_ACCESS_KEY_ID
        kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
    endpoint = (settings.S3_PUBLIC_ENDPOINT_URL or settings.S3_ENDPOINT_URL) if public else settings.S3_ENDPOINT_URL
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client("s3", **kwargs, config=BotoConfig(signature_version="s3v4"))


@router.post("", response_model=UploadResponse)
async def request_upload_url(
    body:   UploadRequest,
    device: Device = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
):
    try:
        ss_uuid = uuid.UUID(body.screenshot_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid screenshot_id")

    s3_key = f"screenshots/{device.company_id}/{device.id}/{ss_uuid}.png"

    try:
        url = _s3_client(public=True).generate_presigned_url(
            "put_object",
            Params={"Bucket": settings.S3_BUCKET, "Key": s3_key, "ContentType": "image/png"},
            ExpiresIn=settings.PRESIGNED_URL_EXPIRY,
        )
    except Exception as e:
        log.error("S3 presigned PUT error: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not generate upload URL")

    try:
        if isinstance(body.captured_at, int):
            captured = datetime.fromtimestamp(body.captured_at, tz=timezone.utc)
        else:
            captured = datetime.fromisoformat(body.captured_at.replace("Z", "+00:00"))
    except (ValueError, OSError):
        captured = datetime.now(timezone.utc)

    # Single round-trip upsert (agents retry uploads, so the row may exist).
    await db.execute(
        pg_insert(Screenshot)
        .values(id=ss_uuid, device_id=device.id, s3_key=s3_key,
                captured_at=captured, file_size=body.file_size)
        .on_conflict_do_update(
            index_elements=["id"],
            set_={"s3_key": s3_key, "captured_at": captured, "file_size": body.file_size},
        )
    )
    await db.commit()

    return UploadResponse(upload_url=url, s3_key=s3_key)


@router.get("/{device_id}", response_model=List[ScreenshotOut])
async def list_screenshots(
    device_id: str,
    limit:     int = Query(default=48, ge=1, le=200),
    offset:    int = Query(default=0, ge=0),
    admin: Employee = Depends(require("screenshots", "view")),
    db: AsyncSession = Depends(get_db),
):
    try:
        device_uuid = uuid.UUID(device_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid device id")

    dev = await db.execute(select(Device).where(Device.id == device_uuid))
    device = dev.scalar_one_or_none()
    if not device or device.company_id != admin.company_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")

    ids = await visible_device_ids(admin, db)
    if ids is not ALL and device.id not in set(ids):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")

    result = await db.execute(
        select(Screenshot)
        .where(Screenshot.device_id == device_uuid)
        .order_by(Screenshot.captured_at.desc())
        .offset(offset).limit(limit)
    )
    s3 = _s3_client()
    out = []
    for ss in result.scalars().all():
        try:
            url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.S3_BUCKET, "Key": ss.s3_key},
                ExpiresIn=3600,
            )
        except Exception:
            url = ""
        out.append(ScreenshotOut(
            id=str(ss.id),
            s3_url=url,
            captured_at=ss.captured_at,
            captured_at_unix=int(ss.captured_at.timestamp()),
            file_size=ss.file_size,
        ))
    return out
