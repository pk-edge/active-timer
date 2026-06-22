"""Agent installer management: upload, list, activate, download, and the
device-facing update-check endpoint that drives agent self-update.

Installers are stored in the same MinIO/S3 bucket as screenshots, under
installers/{company_id}/{platform}/{version}/TimeTracker.{ext}.
"""
import asyncio
import hashlib
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.deps import get_db, get_current_device, require
from app.core import audit
from app.api.v1.endpoints.screenshots import _s3_client
from app.core.config import get_settings
from app.db.models import Employee, Device, InstallerVersion, InstallerDownload, UpdateRun
from app.schemas.rbac import (
    InstallerOut, InstallerDownloadOut,
    InstallerDistributionIn, UpdateReportIn, UpdateRunOut,
)

from datetime import datetime, timezone
from sqlalchemy.dialects.postgresql import insert as pg_insert
from app.core import semver, rollout as rollout_util, manifest as manifest_util
from app.core.release_urls import download_url

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
        rollout_percent=v.rollout_percent if v.rollout_percent is not None else 100,
        mandatory=bool(v.mandatory), channel=v.channel or "stable",
        min_supported_version=v.min_supported_version,
        expires_at=v.expires_at.isoformat() if v.expires_at else None,
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
    # Release the pooled DB connection for the duration of the transfer — a
    # multi-hundred-MB upload takes minutes and must not pin a connection.
    # The session transparently re-acquires one for the INSERT afterwards.
    await db.close()
    try:
        # boto3 upload is blocking — a multi-hundred-MB installer would freeze the
        # event loop (and every other request) for the whole transfer. Run it in a thread.
        await asyncio.to_thread(
            _s3_client().upload_fileobj, file.file, settings.S3_BUCKET, s3_key
        )
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


@router.delete("/{installer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_installer(
    installer_id: str,
    user: Employee = Depends(require("agents", "delete")),
    db: AsyncSession = Depends(get_db),
):
    v = await _get_installer(installer_id, user, db)
    try:
        await asyncio.to_thread(
            _s3_client().delete_object, Bucket=settings.S3_BUCKET, Key=v.s3_key
        )
    except Exception as e:
        log.warning("Could not delete installer from S3 (key=%s): %s", v.s3_key, e)
    await audit.record(db, user, "installer.delete", "installer", str(v.id),
                       {"platform": v.platform, "version": v.version})
    await db.delete(v)
    await db.commit()


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

    # Record who downloaded this installer.
    db.add(InstallerDownload(installer_id=v.id, downloaded_by=user.id))
    await db.commit()

    return {"download_url": url, "version": v.version, "platform": v.platform}


@router.get("/downloads", response_model=list[InstallerDownloadOut])
async def list_downloads(
    user: Employee = Depends(require("agents", "view")),
    db: AsyncSession = Depends(get_db),
):
    """Return all download records for this company, newest first."""
    rows = await db.execute(
        select(InstallerDownload)
        .join(InstallerDownload.installer)
        .where(InstallerVersion.company_id == user.company_id)
        .options(
            joinedload(InstallerDownload.installer),
            joinedload(InstallerDownload.employee),
        )
        .order_by(InstallerDownload.downloaded_at.desc())
        .limit(500)
    )
    results = []
    for dl in rows.scalars().unique().all():
        emp = dl.employee
        inv = dl.installer
        results.append(InstallerDownloadOut(
            id=str(dl.id),
            installer_id=str(inv.id),
            platform=inv.platform,
            version=inv.version,
            installer_type=inv.installer_type,
            downloaded_by_id=str(emp.id),
            downloaded_by_name=emp.name,
            downloaded_by_email=emp.email,
            downloaded_at=dl.downloaded_at.isoformat(),
        ))
    return results


@updates_router.get("/check")
async def check_update(
    device: Device = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
):
    """Agent update check. Backward-compatible: original keys are always present;
    new keys (mandatory/min_supported_version/channel/manifest/signature) are
    additive. The decision (semver + rollout + mandatory + min-version + expiry)
    is made server-side, so old agents keep working via the original keys."""
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
    if not active:
        return {"update_available": False}

    # Expiry guard: never serve a stale release.
    if active.expires_at and active.expires_at < datetime.now(timezone.utc):
        return {"update_available": False}

    current = device.agent_version or ""
    below_min = not semver.meets_minimum(current, active.min_supported_version)
    if not semver.is_newer(active.version, current):
        return {"update_available": False}

    # None-safe: a legitimate rollout_percent of 0 must NOT fall back to 100.
    rp = active.rollout_percent if active.rollout_percent is not None else 100
    eligible = (
        active.mandatory
        or below_min
        or rollout_util.in_rollout(str(device.id), active.version, rp)
    )
    if not eligible:
        return {"update_available": False}

    try:
        url = download_url(active.s3_key)
    except Exception as e:
        log.error("Update URL error: %s", e)
        return {"update_available": False}

    # Use a cached signed manifest if present; otherwise sign on the fly so the
    # response is always verifiable even for rows published before manifest cols.
    if active.manifest_json and active.manifest_sig:
        canonical, sig = active.manifest_json, active.manifest_sig
    else:
        canonical, sig = manifest_util.signed_manifest(
            version=active.version, company_id=active.company_id, platform=active.platform,
            installer_type=active.installer_type, sha256=active.sha256 or "", size=active.file_size or 0,
            channel=active.channel or "stable", mandatory=bool(active.mandatory),
            min_supported_version=active.min_supported_version,
            release_date=(active.published_at or active.uploaded_at).isoformat(),
            expires_at=active.expires_at.isoformat() if active.expires_at else None,
            filename=active.s3_key.rsplit("/", 1)[-1],
        )
    import json as _json
    return {
        "update_available": True,
        "version": active.version,
        "installer_type": active.installer_type,
        "download_url": url,
        "sha256": active.sha256,
        "mandatory": bool(active.mandatory),
        "min_supported_version": active.min_supported_version,
        "channel": active.channel or "stable",
        "manifest": _json.loads(canonical),
        "signature": sig,
        "manifest_url": f"/v1/updates/manifest/{active.id}",
        "public_key_id": "v1",
    }


def _signed_manifest_for(v: InstallerVersion) -> tuple[str, str]:
    """Return (canonical_json, signature) for an installer row, using the cached
    signed manifest when present, otherwise signing on the fly."""
    if v.manifest_json and v.manifest_sig:
        return v.manifest_json, v.manifest_sig
    return manifest_util.signed_manifest(
        version=v.version, company_id=v.company_id, platform=v.platform,
        installer_type=v.installer_type, sha256=v.sha256 or "", size=v.file_size or 0,
        channel=v.channel or "stable", mandatory=bool(v.mandatory),
        min_supported_version=v.min_supported_version,
        release_date=(v.published_at or v.uploaded_at).isoformat(),
        expires_at=v.expires_at.isoformat() if v.expires_at else None,
        filename=v.s3_key.rsplit("/", 1)[-1],
    )


@updates_router.get("/manifest/{installer_id}")
async def get_manifest(
    installer_id: uuid.UUID,
    device: Device = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
):
    row = await db.execute(
        select(InstallerVersion).where(
            InstallerVersion.id == installer_id,
            InstallerVersion.company_id == device.company_id,
        )
    )
    v = row.scalar_one_or_none()
    if not v:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Manifest not found")
    import json as _json
    canonical, sig = _signed_manifest_for(v)
    return {"manifest": _json.loads(canonical), "signature": sig}


@updates_router.post("/report", status_code=202)
async def report_update(
    body: UpdateReportIn,
    device: Device = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
):
    """Agent telemetry. Lenient + idempotent: upsert by (device_id, target_version).
    Never 5xx the agent into a retry loop."""
    try:
        stmt = pg_insert(UpdateRun).values(
            id=uuid.uuid4(), device_id=device.id, company_id=device.company_id,
            from_version=body.from_version, target_version=body.target_version,
            platform=body.platform, channel=body.channel, status=body.status,
            error_code=body.error_code, progress=body.progress or 0,
        ).on_conflict_do_update(
            index_elements=["device_id", "target_version"],
            set_={"status": body.status, "error_code": body.error_code,
                  "progress": body.progress or 0, "from_version": body.from_version,
                  "updated_at": datetime.now(timezone.utc)},
        )
        await db.execute(stmt)
        await db.commit()
    except Exception as e:
        log.warning("update report ignored: %s", e)
    return {"ok": True}


@updates_router.get("/runs")
async def list_update_runs(
    user: Employee = Depends(require("agents", "view")),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.execute(
        select(UpdateRun).where(UpdateRun.company_id == user.company_id)
        .order_by(UpdateRun.updated_at.desc()).limit(500)
    )
    runs = [
        UpdateRunOut(
            id=str(r.id), device_id=str(r.device_id), from_version=r.from_version,
            target_version=r.target_version, status=r.status, error_code=r.error_code,
            progress=r.progress, updated_at=r.updated_at.isoformat() if r.updated_at else None,
        ).model_dump()
        for r in rows.scalars().all()
    ]
    adoption_rows = await db.execute(
        select(Device.agent_version).where(Device.company_id == user.company_id)
    )
    adoption: dict[str, int] = {}
    for (v,) in adoption_rows.all():
        adoption[v or "unknown"] = adoption.get(v or "unknown", 0) + 1
    return {"runs": runs, "adoption": adoption}


@router.patch("/{installer_id}")
async def patch_installer_distribution(
    installer_id: uuid.UUID,
    body: InstallerDistributionIn,
    user: Employee = Depends(require("agents", "edit")),
    db: AsyncSession = Depends(get_db),
):
    row = await db.execute(
        select(InstallerVersion).where(
            InstallerVersion.id == installer_id,
            InstallerVersion.company_id == user.company_id,
        )
    )
    v = row.scalar_one_or_none()
    if not v:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Installer not found")

    if body.rollout_percent is not None:
        v.rollout_percent = max(0, min(100, body.rollout_percent))
    if body.mandatory is not None:
        v.mandatory = body.mandatory
    if body.channel is not None:
        v.channel = body.channel
    if body.min_supported_version is not None:
        v.min_supported_version = body.min_supported_version or None
    if body.expires_at is not None:
        v.expires_at = (
            datetime.fromisoformat(body.expires_at.replace("Z", "+00:00"))
            if body.expires_at else None
        )
    if v.published_at is None:
        v.published_at = datetime.now(timezone.utc)

    canonical, sig = manifest_util.signed_manifest(
        version=v.version, company_id=v.company_id, platform=v.platform,
        installer_type=v.installer_type, sha256=v.sha256 or "", size=v.file_size or 0,
        channel=v.channel or "stable", mandatory=bool(v.mandatory),
        min_supported_version=v.min_supported_version,
        release_date=(v.published_at or v.uploaded_at).isoformat(),
        expires_at=v.expires_at.isoformat() if v.expires_at else None,
        filename=v.s3_key.rsplit("/", 1)[-1],
    )
    v.manifest_json, v.manifest_sig = canonical, sig
    await db.commit()
    return {"id": str(v.id), "rollout_percent": v.rollout_percent, "mandatory": v.mandatory,
            "channel": v.channel, "min_supported_version": v.min_supported_version,
            "expires_at": v.expires_at.isoformat() if v.expires_at else None}
