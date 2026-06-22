# Agent Auto-Update — Backend (SP1 + SP2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add server-side release intelligence (semver compare, staged rollout, mandatory/min-version/channel/expiry), a signed manifest, CDN-ready URLs, and update telemetry to the backend — entirely additively, so existing rows, endpoints, and old agents behave exactly as today.

**Architecture:** Extend `installer_versions` with nullable columns (today-equivalent defaults) + add an `update_runs` table (migration 016). Add pure-Python utilities (`semver`, `rollout`, `update_signing`, `release_urls`, `manifest`). Enhance `GET /v1/updates/check` with additive fields + the new decision pipeline, and add `GET /v1/updates/manifest/{id}`, `POST /v1/updates/report`, `GET /v1/updates/runs`, and `PATCH /v1/installers/{id}`.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, asyncpg, Postgres, `cryptography` (Ed25519), pytest + pytest-asyncio, Docker Compose.

## Global Constraints

- **Additive only.** No existing column, endpoint response key, or behavior may change. New columns are nullable with defaults that reproduce today's behavior (`rollout_percent=100`, `mandatory=false`, `channel='stable'`). `GET /v1/updates/check` keeps all current keys; new keys are additive. The existing test `tests/test_e2eApplication.py::test_13_installers_updates_company` MUST stay green.
- **Python 3.12** runtime (Docker `python:3.12-slim`). Do NOT run pytest on the host (host Python can't build pinned deps).
- **Tests run against live Postgres** via `docker compose run --rm --no-deps backend python -m pytest …`. The DB must already be migrated (a normal `docker compose up -d --build backend` runs `init_db` which applies new `*.sql` migrations).
- **Company isolation:** every new query/endpoint is scoped by `company_id` (device JWT or `require("agents", …)` RBAC).
- **Migrations** are plain `.sql` files in `backend/app/db/migrations/`, applied in filename order and recorded in `schema_migrations`. Use `IF NOT EXISTS` everywhere. Do NOT use `CREATE INDEX CONCURRENTLY` (runs inside the migration transaction).
- **Semver:** versions like `1.2.0`; comparison is numeric tuple, not string. Unparseable versions fall back to string inequality (no regression).

---

### Task 1: Migration 016 — additive columns + `update_runs` table + models

**Files:**
- Create: `backend/app/db/migrations/016_agent_releases.sql`
- Modify: `backend/app/db/models.py` (add columns to `InstallerVersion`; add `UpdateRun` model)
- Test: `backend/tests/test_agent_update.py`

**Interfaces:**
- Produces: new columns `installer_versions.rollout_percent:int, mandatory:bool, channel:str, min_supported_version:str|None, manifest_json:str|None, manifest_sig:str|None, expires_at:datetime|None, published_at:datetime|None`; new table `update_runs` + model `UpdateRun(id, device_id, company_id, from_version, target_version, platform, channel, status, error_code, progress, created_at, updated_at)`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_agent_update.py
import pytest
from sqlalchemy import text as sa_text


@pytest.mark.asyncio
async def test_016_schema_additive(db_session):
    # New installer_versions columns exist with today-equivalent defaults.
    cols = await db_session.execute(sa_text("""
        SELECT column_name, column_default, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'installer_versions'
    """))
    by_name = {c.column_name: c for c in cols.all()}
    for col in ("rollout_percent", "mandatory", "channel", "min_supported_version",
                "manifest_json", "manifest_sig", "expires_at", "published_at"):
        assert col in by_name, f"missing column {col}"
    assert by_name["rollout_percent"].column_default.startswith("100")
    assert "false" in by_name["mandatory"].column_default.lower()

    # update_runs table exists.
    t = await db_session.execute(sa_text("SELECT to_regclass('public.update_runs') AS t"))
    assert t.scalar_one() == "update_runs"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_agent_update.py::test_016_schema_additive -v`
Expected: FAIL (columns/table do not exist yet).

- [ ] **Step 3: Create the migration**

```sql
-- backend/app/db/migrations/016_agent_releases.sql
-- Additive auto-update release intelligence + telemetry. Existing rows keep
-- today's behavior via defaults (rollout_percent=100, mandatory=false, stable).

ALTER TABLE installer_versions
    ADD COLUMN IF NOT EXISTS rollout_percent       INT          NOT NULL DEFAULT 100,
    ADD COLUMN IF NOT EXISTS mandatory             BOOLEAN      NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS channel               VARCHAR(20)  NOT NULL DEFAULT 'stable',
    ADD COLUMN IF NOT EXISTS min_supported_version VARCHAR(50),
    ADD COLUMN IF NOT EXISTS manifest_json         TEXT,
    ADD COLUMN IF NOT EXISTS manifest_sig          TEXT,
    ADD COLUMN IF NOT EXISTS expires_at            TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS published_at          TIMESTAMPTZ;

-- NOTE: no DB CHECK on rollout_percent — the migration runner (app/db/base.py
-- `_statements`) splits files on ';' and cannot parse a dollar-quoted DO block.
-- rollout_percent is clamped to [0,100] in app code (PATCH endpoint + release tool).

CREATE TABLE IF NOT EXISTS update_runs (
    id             UUID PRIMARY KEY,
    device_id      UUID NOT NULL REFERENCES devices(id)   ON DELETE CASCADE,
    company_id     UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    from_version   VARCHAR(50),
    target_version VARCHAR(50) NOT NULL,
    platform       VARCHAR(20),
    channel        VARCHAR(20),
    status         VARCHAR(40) NOT NULL,
    error_code     VARCHAR(100),
    progress       INT NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_update_runs_company_created ON update_runs (company_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_update_runs_device         ON update_runs (device_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_update_run_device_target ON update_runs (device_id, target_version);
```

- [ ] **Step 4: Add the model columns + `UpdateRun` model**

In `backend/app/db/models.py`, add to the `InstallerVersion` class (after `uploaded_at`):

```python
    rollout_percent:       Mapped[int]              = mapped_column(Integer, default=100)
    mandatory:             Mapped[bool]             = mapped_column(Boolean, default=False)
    channel:               Mapped[str]              = mapped_column(String, default="stable")
    min_supported_version: Mapped[str | None]       = mapped_column(String, nullable=True)
    manifest_json:         Mapped[str | None]       = mapped_column(Text, nullable=True)
    manifest_sig:          Mapped[str | None]       = mapped_column(Text, nullable=True)
    expires_at:            Mapped[datetime | None]  = mapped_column(DateTime(timezone=True), nullable=True)
    published_at:          Mapped[datetime | None]  = mapped_column(DateTime(timezone=True), nullable=True)
```

Add a new model (near `InstallerDownload`):

```python
class UpdateRun(Base):
    __tablename__ = "update_runs"

    id:             Mapped[uuid.UUID]        = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    device_id:      Mapped[uuid.UUID]        = mapped_column(UUID, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    company_id:     Mapped[uuid.UUID]        = mapped_column(UUID, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    from_version:   Mapped[str | None]       = mapped_column(String, nullable=True)
    target_version: Mapped[str]              = mapped_column(String, nullable=False)
    platform:       Mapped[str | None]       = mapped_column(String, nullable=True)
    channel:        Mapped[str | None]       = mapped_column(String, nullable=True)
    status:         Mapped[str]              = mapped_column(String, nullable=False)
    error_code:     Mapped[str | None]       = mapped_column(String, nullable=True)
    progress:       Mapped[int]              = mapped_column(Integer, default=0)
    created_at:     Mapped[datetime]         = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at:     Mapped[datetime]         = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
```

Confirm `Integer`, `Boolean`, `Text`, `DateTime`, `ForeignKey` are imported at the top of `models.py` (add any missing to the existing `from sqlalchemy import …`).

- [ ] **Step 5: Apply the migration + run the test**

Run: `docker compose up -d --build backend` (lifespan `init_db` applies migration 016)
Then: `docker compose run --rm --no-deps backend python -m pytest tests/test_agent_update.py::test_016_schema_additive -v`
Expected: PASS.

- [ ] **Step 6: Confirm backward-compat (existing e2e installer test still green)**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_e2eApplication.py::test_13_installers_updates_company -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/db/migrations/016_agent_releases.sql backend/app/db/models.py backend/tests/test_agent_update.py
git commit -m "feat(updates): migration 016 — additive release columns + update_runs"
```

---

### Task 2: `app/core/semver.py` — version parsing + comparison

**Files:**
- Create: `backend/app/core/semver.py`
- Test: `backend/tests/test_semver.py`

**Interfaces:**
- Produces: `parse_version(s: str) -> tuple[int,int,int] | None`; `is_newer(candidate: str, current: str) -> bool`; `meets_minimum(version: str, floor: str | None) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_semver.py
from app.core.semver import parse_version, is_newer, meets_minimum


def test_parse_and_compare():
    assert parse_version("1.2.0") == (1, 2, 0)
    assert parse_version("1.2") == (1, 2, 0)
    assert parse_version("garbage") is None

    assert is_newer("1.2.0", "1.1.9") is True
    assert is_newer("1.2.0", "1.2.0") is False
    assert is_newer("1.1.0", "1.2.0") is False
    # Unparseable → string-inequality fallback (no regression vs today).
    assert is_newer("beta", "1.0.0") is True
    assert is_newer("1.0.0", "1.0.0") is False


def test_meets_minimum():
    assert meets_minimum("1.0.0", None) is True
    assert meets_minimum("1.0.0", "1.0.0") is True
    assert meets_minimum("0.9.0", "1.0.0") is False
    assert meets_minimum("1.1.0", "1.0.0") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_semver.py -v`
Expected: FAIL (`ModuleNotFoundError: app.core.semver`).

- [ ] **Step 3: Implement**

```python
# backend/app/core/semver.py
"""Tiny semver helper for agent update decisions. Tolerant: returns None for
unparseable input so callers can fall back to string comparison (no regression
vs the previous string-equality update check)."""
from __future__ import annotations

import re

_RE = re.compile(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def parse_version(s: str | None) -> tuple[int, int, int] | None:
    if not s:
        return None
    m = _RE.match(s)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0))


def is_newer(candidate: str | None, current: str | None) -> bool:
    """True if `candidate` should be offered as an update over `current`."""
    pc, pcur = parse_version(candidate), parse_version(current)
    if pc is not None and pcur is not None:
        return pc > pcur
    # Fallback preserves today's behavior: any difference is "newer".
    return (candidate or "") != (current or "")


def meets_minimum(version: str | None, floor: str | None) -> bool:
    """True if `version` is >= `floor`. No floor → always True."""
    if not floor:
        return True
    pv, pf = parse_version(version), parse_version(floor)
    if pv is None or pf is None:
        return True  # can't decide → don't force
    return pv >= pf
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_semver.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/semver.py backend/tests/test_semver.py
git commit -m "feat(updates): add semver compare util"
```

---

### Task 3: `app/core/rollout.py` — deterministic staged-rollout bucketing

**Files:**
- Create: `backend/app/core/rollout.py`
- Test: `backend/tests/test_rollout.py`

**Interfaces:**
- Produces: `in_rollout(device_id: str, version: str, rollout_percent: int) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_rollout.py
from app.core.rollout import in_rollout


def test_rollout_bounds_and_determinism():
    dev = "11111111-1111-1111-1111-111111111111"
    assert in_rollout(dev, "1.2.0", 100) is True       # everyone
    assert in_rollout(dev, "1.2.0", 0) is False         # no one
    # Deterministic / sticky for a (device, version) pair.
    assert in_rollout(dev, "1.2.0", 50) == in_rollout(dev, "1.2.0", 50)


def test_rollout_is_monotonic_as_percent_increases():
    # A device included at P% stays included at any higher percent.
    dev = "22222222-2222-2222-2222-222222222222"
    first_in = next(p for p in range(0, 101) if in_rollout(dev, "1.2.0", p))
    assert all(in_rollout(dev, "1.2.0", p) for p in range(first_in, 101))


def test_rollout_distribution_is_reasonable():
    included = sum(in_rollout(f"dev-{i}", "1.2.0", 25) for i in range(2000))
    assert 400 < included < 600   # ~25% of 2000, allow noise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_rollout.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
# backend/app/core/rollout.py
"""Deterministic, sticky staged-rollout bucketing.

A device's bucket is a stable hash of (device_id, target version), so:
- the same device always gets the same answer for a given target version
  (no flip-flopping as the percent ramps up),
- raising rollout_percent only ever ADDS devices (monotonic).
"""
from __future__ import annotations

import hashlib


def _bucket(device_id: str, version: str) -> int:
    h = hashlib.sha256(f"{device_id}:{version}".encode()).hexdigest()
    return int(h[:8], 16) % 100  # 0..99


def in_rollout(device_id: str, version: str, rollout_percent: int) -> bool:
    if rollout_percent >= 100:
        return True
    if rollout_percent <= 0:
        return False
    return _bucket(device_id, version) < rollout_percent
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_rollout.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/rollout.py backend/tests/test_rollout.py
git commit -m "feat(updates): add deterministic rollout bucketing"
```

---

### Task 4: `app/core/update_signing.py` — Ed25519 manifest signing

**Files:**
- Create: `backend/app/core/update_signing.py`
- Modify: `backend/app/core/config.py` (add `AGENT_SIGNING_PRIVATE_KEY: str = ""`)
- Test: `backend/tests/test_update_signing.py`

**Interfaces:**
- Produces: `sign(data: str) -> str` (base64 sig); `verify(data: str, sig_b64: str) -> bool`; `public_key_b64() -> str`.
- Behavior: if `settings.AGENT_SIGNING_PRIVATE_KEY` is set (base64 of a 32-byte Ed25519 seed) it's used; otherwise a **stable dev key is derived from `JWT_SECRET`** so signing works in dev/test without external provisioning (logs a warning).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_update_signing.py
from app.core import update_signing


def test_sign_verify_roundtrip():
    data = '{"version":"1.2.0"}'
    sig = update_signing.sign(data)
    assert isinstance(sig, str) and sig
    assert update_signing.verify(data, sig) is True


def test_verify_rejects_tampered_payload_and_sig():
    data = '{"version":"1.2.0"}'
    sig = update_signing.sign(data)
    assert update_signing.verify('{"version":"9.9.9"}', sig) is False
    assert update_signing.verify(data, "AAAA") is False


def test_public_key_is_stable_and_b64():
    import base64
    pk = update_signing.public_key_b64()
    assert pk == update_signing.public_key_b64()
    assert len(base64.b64decode(pk)) == 32   # Ed25519 public key is 32 bytes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_update_signing.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Add config field**

In `backend/app/core/config.py`, in the `Settings` class near the Redis/AWS block:

```python
    # ── Agent update signing ─────────────────────────────────────────────────
    # Base64 of a 32-byte Ed25519 seed. Blank in dev → a stable key is derived
    # from JWT_SECRET so signing works without provisioning. Set a real value in
    # production; the matching public key is embedded in the agent build.
    AGENT_SIGNING_PRIVATE_KEY: str = ""
```

- [ ] **Step 4: Implement the signer**

```python
# backend/app/core/update_signing.py
"""Ed25519 signing for agent update manifests. The agent embeds the matching
public key and trusts ONLY it. NEVER raises into request handlers — verify()
returns False on any error."""
from __future__ import annotations

import base64
import hashlib
import logging
from functools import lru_cache

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)

from app.core.config import get_settings

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _private_key() -> Ed25519PrivateKey:
    raw = get_settings().AGENT_SIGNING_PRIVATE_KEY.strip()
    if raw:
        seed = base64.b64decode(raw)
        return Ed25519PrivateKey.from_private_bytes(seed)
    # Dev/test fallback: stable key derived from JWT_SECRET (NOT for prod).
    log.warning("AGENT_SIGNING_PRIVATE_KEY unset — deriving a dev signing key "
                "from JWT_SECRET. Set a real key before production.")
    seed = hashlib.sha256(("agent-update-signing:" + get_settings().JWT_SECRET).encode()).digest()
    return Ed25519PrivateKey.from_private_bytes(seed)


def sign(data: str) -> str:
    sig = _private_key().sign(data.encode("utf-8"))
    return base64.b64encode(sig).decode("ascii")


def verify(data: str, sig_b64: str) -> bool:
    try:
        pub = _private_key().public_key()
        pub.verify(base64.b64decode(sig_b64), data.encode("utf-8"))
        return True
    except Exception:
        return False


def public_key_b64() -> str:
    from cryptography.hazmat.primitives import serialization
    raw = _private_key().public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_update_signing.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/core/update_signing.py backend/app/core/config.py backend/tests/test_update_signing.py
git commit -m "feat(updates): add Ed25519 manifest signing"
```

---

### Task 5: `app/core/release_urls.py` — CDN-ready download URL helper

**Files:**
- Create: `backend/app/core/release_urls.py`
- Modify: `backend/app/core/config.py` (add `CDN_BASE_URL: str = ""`)
- Test: `backend/tests/test_release_urls.py`

**Interfaces:**
- Produces: `download_url(s3_key: str) -> str` — returns `f"{CDN_BASE_URL}/{s3_key}"` when `CDN_BASE_URL` is set, else a presigned S3 GET URL (1h) via the existing `_s3_client(public=True)`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_release_urls.py
import app.core.release_urls as ru


def test_cdn_branch(monkeypatch):
    monkeypatch.setattr(ru.settings, "CDN_BASE_URL", "https://cdn.example.com", raising=False)
    assert ru.download_url("installers/co/macos/1.2.0/TimeTracker.dmg") == \
        "https://cdn.example.com/installers/co/macos/1.2.0/TimeTracker.dmg"


def test_presigned_branch(monkeypatch):
    monkeypatch.setattr(ru.settings, "CDN_BASE_URL", "", raising=False)
    monkeypatch.setattr(ru, "_presign", lambda key: f"https://s3/{key}?sig=abc")
    assert ru.download_url("installers/x.dmg") == "https://s3/installers/x.dmg?sig=abc"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_release_urls.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Add config field**

In `backend/app/core/config.py` (near the S3 block):

```python
    # Optional CDN in front of S3 for release files. Blank → presigned S3 URLs
    # (today's behavior). Set to a CDN origin (e.g. https://cdn.tracker.onedge.co)
    # to serve installers/manifests through the edge later — no code change.
    CDN_BASE_URL: str = ""
```

- [ ] **Step 4: Implement**

```python
# backend/app/core/release_urls.py
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_release_urls.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/core/release_urls.py backend/app/core/config.py backend/tests/test_release_urls.py
git commit -m "feat(updates): add CDN-ready download URL helper"
```

---

### Task 6: `app/core/manifest.py` — canonical manifest builder + signer

**Files:**
- Create: `backend/app/core/manifest.py`
- Test: `backend/tests/test_manifest.py`

**Interfaces:**
- Consumes: `update_signing.sign` (Task 4).
- Produces: `build_canonical(version, company_id, platform, installer_type, sha256, size, *, channel, mandatory, min_supported_version, release_date, expires_at, filename) -> str` (deterministic JSON string); `signed_manifest(**same) -> tuple[str, str]` returning `(canonical_json, signature_b64)`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_manifest.py
import json
from app.core import manifest, update_signing


def _kwargs():
    return dict(
        version="1.2.0", company_id="co", platform="macos", installer_type="dmg",
        sha256="abc", size=123, channel="stable", mandatory=False,
        min_supported_version="1.0.0", release_date="2026-06-18T12:00:00Z",
        expires_at="2026-07-18T12:00:00Z", filename="TimeTracker.dmg",
    )


def test_canonical_is_deterministic_and_sorted():
    a = manifest.build_canonical(**_kwargs())
    b = manifest.build_canonical(**_kwargs())
    assert a == b
    doc = json.loads(a)
    assert doc["version"] == "1.2.0"
    assert doc["files"] == [{"path": "TimeTracker.dmg", "sha256": "abc", "size": 123}]
    assert a == json.dumps(doc, sort_keys=True, separators=(",", ":"))


def test_signed_manifest_verifies():
    canonical, sig = manifest.signed_manifest(**_kwargs())
    assert update_signing.verify(canonical, sig) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_manifest.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
# backend/app/core/manifest.py
"""Canonical (sorted, compact) update manifest + Ed25519 signature. Canonical
form is byte-stable so the signature is reproducible and verifiable by the agent."""
from __future__ import annotations

import json

from app.core import update_signing


def build_canonical(*, version, company_id, platform, installer_type, sha256, size,
                    channel, mandatory, min_supported_version, release_date,
                    expires_at, filename) -> str:
    doc = {
        "schema": 1,
        "version": version,
        "company_id": str(company_id),
        "platform": platform,
        "installer_type": installer_type,
        "channel": channel,
        "mandatory": bool(mandatory),
        "min_supported_version": min_supported_version,
        "release_date": release_date,
        "expires_at": expires_at,
        "files": [{"path": filename, "sha256": sha256, "size": size}],
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


def signed_manifest(**kwargs) -> tuple[str, str]:
    canonical = build_canonical(**kwargs)
    return canonical, update_signing.sign(canonical)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_manifest.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/manifest.py backend/tests/test_manifest.py
git commit -m "feat(updates): add canonical manifest builder + signer"
```

---

### Task 7: Enhance `GET /v1/updates/check` (additive fields + decision pipeline)

**Files:**
- Modify: `backend/app/api/v1/endpoints/installers.py` (the `updates_router.get("/check")` handler)
- Test: `backend/tests/test_agent_update.py` (append)

**Interfaces:**
- Consumes: `semver.is_newer`, `semver.meets_minimum` (T2), `rollout.in_rollout` (T3), `release_urls.download_url` (T5), `manifest` cols on `InstallerVersion` (T1), `update_signing.public_key_b64` (T4).
- Produces: enhanced response — existing keys (`update_available, version, installer_type, download_url, sha256`) plus `mandatory, min_supported_version, channel, manifest, signature, manifest_url, public_key_id`. Decision: offered iff newer-by-semver AND (in rollout OR mandatory OR below min) AND not expired.

- [ ] **Step 1: Write failing tests (decision pipeline + backward-compat)**

```python
# append to backend/tests/test_agent_update.py
import uuid
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import text as sa_text
from app.core.security import create_device_token
from app.core import update_signing


async def _seed_device_and_installer(db, **installer_overrides):
    """Create a throwaway company+device+active installer; returns (headers, company_id, version)."""
    cid = uuid.uuid4(); did = uuid.uuid4(); tag = uuid.uuid4().hex[:8]
    await db.execute(sa_text(
        "INSERT INTO companies (id, name, enrollment_token) VALUES (:i,:n,:t)"),
        {"i": str(cid), "n": f"UP-{tag}", "t": tag})
    await db.execute(sa_text(
        "INSERT INTO devices (id, company_id, hostname, platform, agent_version) "
        "VALUES (:i,:c,:h,'darwin','1.0.0')"),
        {"i": str(did), "c": str(cid), "h": f"h-{tag}"})
    fields = dict(version="1.2.0", rollout_percent=100, mandatory=False,
                  channel="stable", min_supported_version=None, expires_at=None)
    fields.update(installer_overrides)
    await db.execute(sa_text(
        "INSERT INTO installer_versions (id, company_id, platform, installer_type, version, "
        "s3_key, file_size, sha256, is_active, rollout_percent, mandatory, channel, "
        "min_supported_version, expires_at) VALUES "
        "(:id,:c,'macos','dmg',:v,:k,123,'deadbeef',TRUE,:rp,:m,:ch,:mn,:ex)"),
        {"id": str(uuid.uuid4()), "c": str(cid), "v": fields["version"],
         "k": f"installers/{cid}/macos/{fields['version']}/TimeTracker.dmg",
         "rp": fields["rollout_percent"], "m": fields["mandatory"], "ch": fields["channel"],
         "mn": fields["min_supported_version"], "ex": fields["expires_at"]})
    await db.flush()
    headers = {"Authorization": f"Bearer {create_device_token(str(did), str(cid))}"}
    return headers, cid, fields["version"]


@pytest.mark.asyncio
async def test_check_backward_compat_keys_present(client, db_session, monkeypatch):
    monkeypatch.setattr("app.core.release_urls.settings.CDN_BASE_URL", "https://cdn.test", raising=False)
    headers, _, _ = await _seed_device_and_installer(db_session, rollout_percent=100)
    r = await client.get("/v1/updates/check", headers=headers)
    assert r.status_code == 200
    body = r.json()
    for k in ("update_available", "version", "installer_type", "download_url", "sha256"):
        assert k in body                       # existing keys intact
    assert body["update_available"] is True
    assert body["version"] == "1.2.0"


@pytest.mark.asyncio
async def test_check_rollout_zero_withholds_nonmandatory(client, db_session, monkeypatch):
    monkeypatch.setattr("app.core.release_urls.settings.CDN_BASE_URL", "https://cdn.test", raising=False)
    headers, _, _ = await _seed_device_and_installer(db_session, rollout_percent=0)
    r = await client.get("/v1/updates/check", headers=headers)
    assert r.json()["update_available"] is False


@pytest.mark.asyncio
async def test_check_mandatory_overrides_rollout(client, db_session, monkeypatch):
    monkeypatch.setattr("app.core.release_urls.settings.CDN_BASE_URL", "https://cdn.test", raising=False)
    headers, _, _ = await _seed_device_and_installer(db_session, rollout_percent=0, mandatory=True)
    body = (await client.get("/v1/updates/check", headers=headers)).json()
    assert body["update_available"] is True and body["mandatory"] is True


@pytest.mark.asyncio
async def test_check_expired_withholds(client, db_session, monkeypatch):
    monkeypatch.setattr("app.core.release_urls.settings.CDN_BASE_URL", "https://cdn.test", raising=False)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    headers, _, _ = await _seed_device_and_installer(db_session, rollout_percent=100, expires_at=past)
    assert (await client.get("/v1/updates/check", headers=headers)).json()["update_available"] is False


@pytest.mark.asyncio
async def test_check_signature_verifies(client, db_session, monkeypatch):
    monkeypatch.setattr("app.core.release_urls.settings.CDN_BASE_URL", "https://cdn.test", raising=False)
    headers, _, _ = await _seed_device_and_installer(db_session, rollout_percent=100)
    body = (await client.get("/v1/updates/check", headers=headers)).json()
    import json as _json
    canonical = _json.dumps(body["manifest"], sort_keys=True, separators=(",", ":"))
    assert update_signing.verify(canonical, body["signature"]) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_agent_update.py -k check -v`
Expected: FAIL (new keys absent / rollout+expiry not honored).

- [ ] **Step 3: Replace the `/check` handler**

In `backend/app/api/v1/endpoints/installers.py`, add imports near the top:

```python
from datetime import datetime, timezone
from app.core import semver, rollout as rollout_util, manifest as manifest_util, update_signing
from app.core.release_urls import download_url
```

Replace the body of the `updates_router.get("/check")` handler with:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_agent_update.py -k check -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Re-run the existing e2e installer test (backward-compat gate)**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_e2eApplication.py::test_13_installers_updates_company -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/endpoints/installers.py backend/tests/test_agent_update.py
git commit -m "feat(updates): enhance /updates/check with rollout, mandatory, min-version, signed manifest"
```

---

### Task 8: `GET /v1/updates/manifest/{id}` — fetch signed manifest

**Files:**
- Modify: `backend/app/api/v1/endpoints/installers.py` (add route to `updates_router`)
- Test: `backend/tests/test_agent_update.py` (append)

**Interfaces:**
- Consumes: `InstallerVersion` row + `manifest_util.signed_manifest` (T6).
- Produces: `GET /v1/updates/manifest/{installer_id}` (device JWT, company-scoped) → `{"manifest": <dict>, "signature": <b64>}`; 404 if not found or other company.

- [ ] **Step 1: Write the failing test**

```python
# append to backend/tests/test_agent_update.py
@pytest.mark.asyncio
async def test_manifest_endpoint_scoped_and_signed(client, db_session, monkeypatch):
    monkeypatch.setattr("app.core.release_urls.settings.CDN_BASE_URL", "https://cdn.test", raising=False)
    headers, cid, _ = await _seed_device_and_installer(db_session, rollout_percent=100)
    row = await db_session.execute(sa_text(
        "SELECT id FROM installer_versions WHERE company_id = :c"), {"c": str(cid)})
    iid = str(row.scalar_one())
    r = await client.get(f"/v1/updates/manifest/{iid}", headers=headers)
    assert r.status_code == 200
    body = r.json()
    import json as _json
    canonical = _json.dumps(body["manifest"], sort_keys=True, separators=(",", ":"))
    assert update_signing.verify(canonical, body["signature"]) is True
    # Unknown id → 404.
    assert (await client.get(f"/v1/updates/manifest/{uuid.uuid4()}", headers=headers)).status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_agent_update.py::test_manifest_endpoint_scoped_and_signed -v`
Expected: FAIL (404 for the valid id — route missing).

- [ ] **Step 3: Implement the route**

Add to `installers.py` (in the `updates_router` section):

```python
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

    if v.manifest_json and v.manifest_sig:
        canonical, sig = v.manifest_json, v.manifest_sig
    else:
        canonical, sig = manifest_util.signed_manifest(
            version=v.version, company_id=v.company_id, platform=v.platform,
            installer_type=v.installer_type, sha256=v.sha256 or "", size=v.file_size or 0,
            channel=v.channel or "stable", mandatory=bool(v.mandatory),
            min_supported_version=v.min_supported_version,
            release_date=(v.published_at or v.uploaded_at).isoformat(),
            expires_at=v.expires_at.isoformat() if v.expires_at else None,
            filename=v.s3_key.rsplit("/", 1)[-1],
        )
    import json as _json
    return {"manifest": _json.loads(canonical), "signature": sig}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_agent_update.py::test_manifest_endpoint_scoped_and_signed -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/endpoints/installers.py backend/tests/test_agent_update.py
git commit -m "feat(updates): add signed manifest endpoint"
```

---

### Task 9: Telemetry — `POST /v1/updates/report` + `GET /v1/updates/runs`

**Files:**
- Modify: `backend/app/api/v1/endpoints/installers.py` (add 2 routes to `updates_router`)
- Modify: `backend/app/schemas/rbac.py` (add `UpdateReportIn`, `UpdateRunOut`)
- Test: `backend/tests/test_agent_update.py` (append)

**Interfaces:**
- Consumes: `UpdateRun` model (T1), `get_current_device`, `require("agents","view")`.
- Produces: `POST /v1/updates/report` (device JWT) upserts a run by `(device_id, target_version)`, advancing status/progress; returns 202. `GET /v1/updates/runs` (RBAC) returns `{"runs": [...], "adoption": {version: count}}` scoped to company.

- [ ] **Step 1: Write failing tests**

```python
# append to backend/tests/test_agent_update.py
from app.core.security import create_admin_token


@pytest.mark.asyncio
async def test_report_upserts_and_runs_list(client, db_session):
    headers, cid, ver = await _seed_device_and_installer(db_session, rollout_percent=100)

    r = await client.post("/v1/updates/report", headers=headers,
                          json={"target_version": ver, "from_version": "1.0.0",
                                "status": "FILE_DOWNLOAD_STARTED", "progress": 10})
    assert r.status_code == 202

    # Same (device, target) advances the SAME row, not a new one.
    r = await client.post("/v1/updates/report", headers=headers,
                          json={"target_version": ver, "status": "INSTALL_SUCCESS", "progress": 100})
    assert r.status_code == 202

    rows = await db_session.execute(sa_text(
        "SELECT status, progress FROM update_runs WHERE company_id = :c"), {"c": str(cid)})
    all_rows = rows.all()
    assert len(all_rows) == 1
    assert all_rows[0].status == "INSTALL_SUCCESS" and all_rows[0].progress == 100


@pytest.mark.asyncio
async def test_runs_read_is_company_scoped(client, db_session):
    headers, cid, ver = await _seed_device_and_installer(db_session, rollout_percent=100)
    await client.post("/v1/updates/report", headers=headers,
                      json={"target_version": ver, "status": "INSTALL_SUCCESS"})
    # Admin in the SAME company can read the runs.
    admin_h = {"Authorization": f"Bearer {create_admin_token(str(uuid.uuid4()), 'admin', str(cid))}"}
    r = await client.get("/v1/updates/runs", headers=admin_h)
    assert r.status_code == 200
    assert any(run["target_version"] == ver for run in r.json()["runs"])
```

> Note: if `require("agents","view")` needs a real employee row, mirror the pattern used in `tests/test_e2eApplication.py` for admin auth (create the admin via `ensure_admin`) instead of a synthetic token. Adjust this test to that pattern if the synthetic admin token is rejected by RBAC.

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_agent_update.py -k "report or runs" -v`
Expected: FAIL (routes missing).

- [ ] **Step 3: Add schemas**

In `backend/app/schemas/rbac.py`:

```python
class UpdateReportIn(BaseModel):
    target_version: str
    from_version: Optional[str] = None
    status: str
    error_code: Optional[str] = None
    progress: Optional[int] = 0
    platform: Optional[str] = None
    channel: Optional[str] = None


class UpdateRunOut(BaseModel):
    id: str
    device_id: str
    from_version: Optional[str] = None
    target_version: str
    status: str
    error_code: Optional[str] = None
    progress: int
    updated_at: Optional[str] = None
```

- [ ] **Step 4: Implement the routes**

In `installers.py` add imports + routes:

```python
from app.db.models import UpdateRun
from app.schemas.rbac import UpdateReportIn, UpdateRunOut
from sqlalchemy.dialects.postgresql import insert as pg_insert
```

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_agent_update.py -k "report or runs" -v`
Expected: PASS (adjust admin-auth per the Step-1 note if RBAC rejects the synthetic token).

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/endpoints/installers.py backend/app/schemas/rbac.py backend/tests/test_agent_update.py
git commit -m "feat(updates): add update telemetry report + runs read endpoints"
```

---

### Task 10: `PATCH /v1/installers/{id}` — distribution config + manifest attach

**Files:**
- Modify: `backend/app/api/v1/endpoints/installers.py` (add PATCH route to `router`)
- Modify: `backend/app/schemas/rbac.py` (add `InstallerDistributionIn`)
- Test: `backend/tests/test_agent_update.py` (append)

**Interfaces:**
- Consumes: `manifest_util.signed_manifest` (T6), `require("agents","edit")`.
- Produces: `PATCH /v1/installers/{id}` (RBAC) sets `rollout_percent/mandatory/channel/min_supported_version/expires_at`, regenerates+stores `manifest_json/manifest_sig`, sets `published_at`. Returns the updated `InstallerOut`-style dict. Consumed later by the release tool (SP3) and dashboard (SP5).

- [ ] **Step 1: Write the failing test**

```python
# append to backend/tests/test_agent_update.py
@pytest.mark.asyncio
async def test_patch_distribution_sets_fields_and_manifest(client, db_session):
    headers, cid, ver = await _seed_device_and_installer(db_session, rollout_percent=100)
    row = await db_session.execute(sa_text(
        "SELECT id FROM installer_versions WHERE company_id = :c"), {"c": str(cid)})
    iid = str(row.scalar_one())
    admin_h = {"Authorization": f"Bearer {create_admin_token(str(uuid.uuid4()), 'admin', str(cid))}"}
    r = await client.patch(f"/v1/installers/{iid}", headers=admin_h,
                           json={"rollout_percent": 25, "mandatory": True, "channel": "beta"})
    assert r.status_code == 200
    saved = await db_session.execute(sa_text(
        "SELECT rollout_percent, mandatory, channel, manifest_json, manifest_sig "
        "FROM installer_versions WHERE id = :i"), {"i": iid})
    s = saved.first()
    assert s.rollout_percent == 25 and s.mandatory is True and s.channel == "beta"
    assert s.manifest_json and s.manifest_sig    # manifest regenerated + signed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_agent_update.py::test_patch_distribution_sets_fields_and_manifest -v`
Expected: FAIL (PATCH route missing → 405).

- [ ] **Step 3: Add schema + route**

In `backend/app/schemas/rbac.py`:

```python
class InstallerDistributionIn(BaseModel):
    rollout_percent: Optional[int] = None
    mandatory: Optional[bool] = None
    channel: Optional[str] = None
    min_supported_version: Optional[str] = None
    expires_at: Optional[str] = None   # ISO8601
```

In `installers.py` (on the admin `router`, not `updates_router`):

```python
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
        v.expires_at = datetime.fromisoformat(body.expires_at.replace("Z", "+00:00")) if body.expires_at else None
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_agent_update.py::test_patch_distribution_sets_fields_and_manifest -v`
Expected: PASS.

- [ ] **Step 5: Run the full new suite + backward-compat gate**

Run: `docker compose run --rm --no-deps backend python -m pytest tests/test_agent_update.py tests/test_semver.py tests/test_rollout.py tests/test_update_signing.py tests/test_release_urls.py tests/test_manifest.py tests/test_e2eApplication.py::test_13_installers_updates_company -v`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/endpoints/installers.py backend/app/schemas/rbac.py backend/tests/test_agent_update.py
git commit -m "feat(updates): add PATCH installer distribution config + manifest attach"
```

---

## Self-Review

**1. Spec coverage (SP1 + SP2):**
- §5.1 additive columns + §6.1 `update_runs` → Task 1 ✓
- §5.4 semver compare → Task 2 ✓; rollout bucketing → Task 3 ✓; mandatory/min-version/expiry → Task 7 ✓
- §5.3 Ed25519 signing → Task 4 ✓; §5.2 canonical manifest → Task 6 ✓
- §10 CDN-ready URL → Task 5 ✓
- §5.4 enhanced `/check` (backward-compatible) → Task 7 ✓; §5.5 manifest endpoint → Task 8 ✓
- §6.3 `POST /report` + §6.4 `GET /runs` (+ adoption) → Task 9 ✓
- §9 distribution config the dashboard/tool drive → Task 10 ✓ (consumed by SP3/SP5)
- §5.6 rollback = re-activate previous (existing activate endpoint) — no new code; documented.

**2. Placeholder scan:** No TBD/TODO; every code step has complete code. The one judgment note (Task 9 admin-auth) gives an exact fallback (mirror `ensure_admin`), not a vague "handle it."

**3. Type consistency:** `signed_manifest(**kwargs)` keyword set is identical in Tasks 6, 7, 8, 10. `in_rollout(device_id, version, rollout_percent)`, `is_newer(candidate, current)`, `meets_minimum(version, floor)`, `download_url(s3_key)`, `verify(data, sig)`, `public_key_b64()` are used with the same signatures everywhere. `UpdateRun` columns match the migration. Response keys in Task 7 are a superset of today's.

## Execution Handoff

This plan (Plan A — Backend) is one of four; SP3 (release tool), SP4 (agent client), SP5 (dashboard) get their own plans after this lands and its interfaces are real.
