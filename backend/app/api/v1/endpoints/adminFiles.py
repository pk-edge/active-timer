"""Admin file sharing: upload files to S3 and distribute permanent public download links.

Each uploaded file gets a 64-character random token. The public download endpoint
at GET /files/public/{token} requires no auth and redirects to a 1-hour presigned
S3 URL — so the shareable link is permanent (backed by the DB record) but the
actual S3 access still flows through short-lived presigned URLs.
"""
import asyncio
import logging
import re
import secrets
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require
# Reuse the SAME S3 client the agent installer download uses (release_urls →
# screenshots._s3_client). public=True signs against S3_PUBLIC_ENDPOINT_URL, the
# host the browser can reach; public=False uses the internal endpoint.
from app.api.v1.endpoints.screenshots import _s3_client
from app.core.config import get_settings
from app.db.models import Employee, SharedFile

log = logging.getLogger(__name__)
router = APIRouter()
settings = get_settings()

# Fallback used when the company hasn't configured a limit (or the settings
# column is unreadable on a pre-migration DB). The effective limit is the
# company-wide `max_file_size_mb` setting — configurable from the dashboard.
_DEFAULT_MAX_FILE_MB = 500


async def _company_max_file_mb(db: AsyncSession, company_id) -> int:
    """Read the company's configured max upload size (MB) from companies.settings.
    Degrades to the default if the column/key is absent or invalid."""
    try:
        stored = (await db.execute(
            text("SELECT settings FROM companies WHERE id = :id"),
            {"id": str(company_id)},
        )).scalar()
    except Exception:  # noqa: BLE001 — pre-migration DB: fall back gracefully
        await db.rollback()
        return _DEFAULT_MAX_FILE_MB
    if isinstance(stored, dict):
        n = stored.get("max_file_size_mb")
        if isinstance(n, int) and not isinstance(n, bool) and n > 0:
            return n
    return _DEFAULT_MAX_FILE_MB


def _safe_filename(name: str | None) -> str:
    """Sanitize a user-supplied filename for storage keys and the
    Content-Disposition header. Quotes, control chars, and path separators are
    stripped — a raw '"' or CR/LF here would let an uploader inject arbitrary
    headers into the presigned download response."""
    cleaned = re.sub(r'[^A-Za-z0-9._ ()\[\]-]+', "_", (name or "file")).strip(" .")
    return cleaned[:150] or "file"


class SharedFileOut(BaseModel):
    id: str
    original_filename: str
    mime_type: str | None
    file_size_bytes: int
    public_token: str
    created_at: str
    uploaded_by_name: str | None


def _out(f: SharedFile) -> SharedFileOut:
    return SharedFileOut(
        id=str(f.id),
        original_filename=f.original_filename,
        mime_type=f.mime_type,
        file_size_bytes=f.file_size_bytes,
        public_token=f.public_token,
        created_at=f.created_at.isoformat() if f.created_at else "",
        uploaded_by_name=f.uploader.name if f.uploader else None,
    )


# ── Public download (no auth) ─────────────────────────────────────────────────

@router.get("/public/{token}")
async def download_public_file(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """Anyone with the link can download. Redirects to a 1-hour presigned S3 URL."""
    row = await db.execute(select(SharedFile).where(SharedFile.public_token == token))
    f = row.scalar_one_or_none()
    if not f:
        raise HTTPException(status_code=404, detail="File not found or link has expired")

    try:
        presigned = await asyncio.to_thread(
            _s3_client(public=True).generate_presigned_url,
            "get_object",
            Params={
                "Bucket": settings.S3_BUCKET,
                "Key": f.s3_key,
                "ResponseContentDisposition": f'attachment; filename="{_safe_filename(f.original_filename)}"',
            },
            ExpiresIn=3600,
        )
    except Exception as exc:
        log.error("Presign failed for token %s: %s", token[:8], exc)
        raise HTTPException(status_code=500, detail="Could not generate download link")

    return RedirectResponse(url=presigned, status_code=302)


# ── Admin CRUD (requires auth) ────────────────────────────────────────────────

class FileLimits(BaseModel):
    max_file_size_mb: int


@router.get("/limits", response_model=FileLimits)
async def get_file_limits(
    admin: Employee = Depends(require("files", "view")),
    db: AsyncSession = Depends(get_db),
):
    """The current company-wide upload size limit, so the dashboard can display
    it and validate before uploading."""
    return FileLimits(max_file_size_mb=await _company_max_file_mb(db, admin.company_id))


class SharedFilesPage(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[SharedFileOut]


@router.get("", response_model=SharedFilesPage)
async def list_shared_files(
    limit:  int         = Query(default=20, ge=1, le=100),
    offset: int         = Query(default=0, ge=0),
    q:      str | None  = Query(default=None, description="Smart search: space-separated tokens matched against filename, uploader name, and file type"),
    admin: Employee = Depends(require("files", "view")),
    db: AsyncSession = Depends(get_db),
):
    """Paginated list with optional multi-token search.

    Each space-separated word in `q` must independently match at least one
    searchable field — so "john pdf" returns PDFs uploaded by John. All tokens
    are applied as AND conditions; matching is case-insensitive and partial.
    """
    params: dict = {"cid": str(admin.company_id), "limit": limit, "offset": offset}
    filters = ["sf.company_id = :cid"]

    tokens = [t.strip() for t in (q or "").split() if t.strip()]
    for i, tok in enumerate(tokens[:8]):
        key = f"tok{i}"
        params[key] = f"%{tok}%"
        filters.append(f"""(
            sf.original_filename ILIKE :{key}
            OR e.name ILIKE :{key}
            OR sf.mime_type ILIKE :{key}
        )""")

    where = " AND ".join(filters)

    count_res = await db.execute(
        text(f"""
            SELECT COUNT(*)
            FROM shared_files sf
            LEFT JOIN employees e ON e.id = sf.uploaded_by
            WHERE {where}
        """),
        params,
    )
    total = int(count_res.scalar() or 0)

    rows_res = await db.execute(
        text(f"""
            SELECT sf.id, sf.original_filename, sf.mime_type, sf.file_size_bytes,
                   sf.public_token, sf.created_at, e.name AS uploader_name
            FROM shared_files sf
            LEFT JOIN employees e ON e.id = sf.uploaded_by
            WHERE {where}
            ORDER BY sf.created_at DESC
            LIMIT :limit OFFSET :offset
        """),
        params,
    )
    items = [
        SharedFileOut(
            id=str(r.id),
            original_filename=r.original_filename,
            mime_type=r.mime_type,
            file_size_bytes=r.file_size_bytes,
            public_token=r.public_token,
            created_at=r.created_at.isoformat() if r.created_at else "",
            uploaded_by_name=r.uploader_name,
        )
        for r in rows_res.fetchall()
    ]
    return SharedFilesPage(total=total, offset=offset, limit=limit, items=items)


@router.post("", response_model=SharedFileOut, status_code=status.HTTP_201_CREATED)
async def upload_shared_file(
    file: UploadFile = File(...),
    admin: Employee = Depends(require("files", "create")),
    db: AsyncSession = Depends(get_db),
):
    # Stream-read to measure size; reject oversized files before touching S3.
    # The limit is company-configurable (defaults to 500 MB).
    max_mb = await _company_max_file_mb(db, admin.company_id)
    size = 0
    max_bytes = max_mb * 1024 * 1024
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > max_bytes:
            raise HTTPException(status_code=413, detail=f"File exceeds {max_mb} MB limit")
    await file.seek(0)

    token = secrets.token_hex(32)
    safe_name = _safe_filename(file.filename)
    s3_key = f"shared-files/{admin.company_id}/{token[:8]}_{safe_name}"

    # Release the pooled DB connection while the S3 transfer runs — large files
    # take seconds and must not pin a connection from the pool.
    await db.close()
    try:
        await asyncio.to_thread(
            _s3_client().upload_fileobj,
            file.file,
            settings.S3_BUCKET,
            s3_key,
            ExtraArgs={"ContentType": file.content_type or "application/octet-stream"},
        )
    except Exception as exc:
        log.error("Shared file upload to S3 failed: %s", exc)
        raise HTTPException(status_code=500, detail="Upload to storage failed")

    shared = SharedFile(
        company_id=admin.company_id,
        uploaded_by=admin.id,
        original_filename=file.filename or "file",
        s3_key=s3_key,
        mime_type=file.content_type,
        file_size_bytes=size,
        public_token=token,
    )
    db.add(shared)
    await db.commit()
    await db.refresh(shared)
    return _out(shared)


@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_shared_file(
    file_id: str,
    admin: Employee = Depends(require("files", "delete")),
    db: AsyncSession = Depends(get_db),
):
    try:
        fid = uuid.UUID(file_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid file id")

    row = await db.execute(
        select(SharedFile).where(
            SharedFile.id == fid,
            SharedFile.company_id == admin.company_id,
        )
    )
    f = row.scalar_one_or_none()
    if not f:
        raise HTTPException(status_code=404, detail="File not found")

    try:
        await asyncio.to_thread(
            _s3_client().delete_object,
            Bucket=settings.S3_BUCKET,
            Key=f.s3_key,
        )
    except Exception as exc:
        log.warning("S3 delete failed for key %s: %s", f.s3_key, exc)

    await db.delete(f)
    await db.commit()
