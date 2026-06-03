"""Agent installer management: upload, list, activate, download, and the
device-facing update-check endpoint that drives agent self-update.

Installers are stored in the same MinIO/S3 bucket as screenshots, under
installers/{company_id}/{platform}/{version}/TimeTracker.{ext}.
"""
import hashlib
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_current_device, require
from app.core import audit
from app.api.v1.endpoints.screenshots import _s3_client
from app.core.config import get_settings
from app.db.models import Employee, Device, InstallerVersion
from app.schemas.rbac import InstallerOut

log = logging.getLogger(__name__)
router = APIRouter()          # mounted at /installers
updates_router = APIRouter()  # mounted at /updates
settings = get_settings()

_EXT = {"dmg": "dmg", "msi": "msi", "exe": "exe"}
_PLATFORM_OK = {"macos", "windows"}
# Agent reports sys.platform.lower(); map to installer platform.
_AGENT_PLATFORM = {"darwin": "macos", "win32": "windows"}
_TYPE_FOR_PLATFORM = {"macos": ("dmg",), "windows": ("msi", "exe")}


def _out(v: InstallerVersion) -> InstallerOut:
    return InstallerOut(
        id=str(v.id), platform=v.platform, installer_type=v.installer_type,
        version=v.version, file_size=v.file_size, sha256=v.sha256, notes=v.notes,
        is_active=v.is_active, uploaded_at=v.uploaded_at.isoformat() if v.uploaded_at else None,
    )


@router.get("", response_model=list[InstallerOut])
async def list_installers(
    user: Employee = Depends(require("agents", "view")),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.execute(
        select(InstallerVersion).where(InstallerVersion.company_id == user.company_id)
        .order_by(InstallerVersion.uploaded_at.desc())
    )
    return [_out(v) for v in rows.scalars().all()]


@router.post("", response_model=InstallerOut, status_code=status.HTTP_201_CREATED)
async def upload_installer(
    platform: str = Form(...),
    installer_type: str = Form(...),
    version: str = Form(...),
    notes: str | None = Form(default=None),
    file: UploadFile = File(...),
    user: Employee = Depends(require("agents", "create")),
    db: AsyncSession = Depends(get_db),
):
    platform = platform.lower().strip()
    installer_type = installer_type.lower().strip()
    if platform not in _PLATFORM_OK:
        raise HTTPException(status_code=400, detail="platform must be 'macos' or 'windows'")
    if installer_type not in _TYPE_FOR_PLATFORM[platform]:
        raise HTTPException(status_code=400, detail=f"{platform} expects {_TYPE_FOR_PLATFORM[platform]}")

    # Stream-hash + size check before upload.
    sha = hashlib.sha256()
    size = 0
    max_bytes = settings.MAX_INSTALLER_MB * 1024 * 1024
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > max_bytes:
            raise HTTPException(status_code=413, detail=f"File exceeds {settings.MAX_INSTALLER_MB} MB")
        sha.update(chunk)
    await file.seek(0)

    s3_key = f"installers/{user.company_id}/{platform}/{version}/TimeTracker.{_EXT[installer_type]}"
    try:
        _s3_client().upload_fileobj(file.file, settings.S3_BUCKET, s3_key)
    except Exception as e:
        log.error("Installer upload failed: %s", e)
        raise HTTPException(status_code=500, detail="Upload to storage failed")

    v = InstallerVersion(
        company_id=user.company_id, platform=platform, installer_type=installer_type,
        version=version, s3_key=s3_key, file_size=size, sha256=sha.hexdigest(),
        notes=notes, is_active=False, uploaded_by=user.id,
    )
    db.add(v)
    await db.flush()
    await audit.record(db, user, "installer.upload", "installer", str(v.id),
                       {"platform": platform, "version": version})
    await db.commit()
    await db.refresh(v)
    return _out(v)


async def _get_installer(installer_id: str, user: Employee, db: AsyncSession) -> InstallerVersion:
    try:
        iid = uuid.UUID(installer_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid installer id")
    row = await db.execute(
        select(InstallerVersion).where(
            InstallerVersion.id == iid, InstallerVersion.company_id == user.company_id)
    )
    v = row.scalar_one_or_none()
    if not v:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Installer not found")
    return v


@router.post("/{installer_id}/activate", response_model=InstallerOut)
async def activate_installer(
    installer_id: str,
    user: Employee = Depends(require("agents", "edit")),
    db: AsyncSession = Depends(get_db),
):
    v = await _get_installer(installer_id, user, db)
    # Deactivate other versions for this platform first (one active per platform).
    await db.execute(
        update(InstallerVersion)
        .where(
            InstallerVersion.company_id == user.company_id,
            InstallerVersion.platform == v.platform,
            InstallerVersion.id != v.id,
        )
        .values(is_active=False)
    )
    await db.flush()
    v.is_active = True
    await audit.record(db, user, "installer.activate", "installer", str(v.id),
                       {"platform": v.platform, "version": v.version})
    await db.commit()
    await db.refresh(v)
    return _out(v)


@router.get("/{installer_id}/download")
async def download_installer(
    installer_id: str,
    user: Employee = Depends(require("agents", "view")),
    db: AsyncSession = Depends(get_db),
):
    v = await _get_installer(installer_id, user, db)
    try:
        url = _s3_client(public=True).generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.S3_BUCKET, "Key": v.s3_key},
            ExpiresIn=3600,
        )
    except Exception as e:
        log.error("Presigned download error: %s", e)
        raise HTTPException(status_code=500, detail="Could not generate download URL")
    return {"download_url": url, "version": v.version, "platform": v.platform}


@updates_router.get("/check")
async def check_update(
    device: Device = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
):
    """Agent calls this with its device JWT. Returns the active installer for the
    device's platform if its version differs from the agent's current version."""
    plat = _AGENT_PLATFORM.get((device.platform or "").lower())
    if not plat:
        return {"update_available": False}

    row = await db.execute(
        select(InstallerVersion).where(
            InstallerVersion.company_id == device.company_id,
            InstallerVersion.platform == plat,
            InstallerVersion.is_active == True,
        )
    )
    active = row.scalar_one_or_none()
    if not active or active.version == (device.agent_version or ""):
        return {"update_available": False}

    try:
        url = _s3_client(public=True).generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.S3_BUCKET, "Key": active.s3_key},
            ExpiresIn=3600,
        )
    except Exception as e:
        log.error("Update presign error: %s", e)
        return {"update_available": False}

    return {
        "update_available": True,
        "version": active.version,
        "installer_type": active.installer_type,
        "download_url": url,
        "sha256": active.sha256,
    }
