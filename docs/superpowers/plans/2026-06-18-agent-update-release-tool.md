# Agent Auto-Update — Release Tool (SP3) Implementation Plan

> **For agentic workers:** execute task-by-task (TDD). Steps use checkbox syntax.

**Goal:** A local CLI (`installer/release.py`) that publishes a built installer: upload → set distribution (rollout/mandatory/channel/min-version/expiry) → activate, by orchestrating existing backend endpoints. The manifest is signed **server-side** by the PATCH endpoint, so no signing key lives in the tool.

**Architecture:** Thin `httpx` client. Pure helpers (`derive_installer_type`, `build_patch_payload`) + an injectable `publish(client, …)` orchestrator (testable with a fake client). Existing build scripts (`build_macos.sh`, `build_windows.ps1`) are unchanged; this runs after them.

**Tech Stack:** Python 3.12, httpx (already a backend dep), argparse, stdlib.

## Global Constraints
- Additive: no backend or build-script change. Uses existing `POST /v1/installers`, `POST /v1/installers/{id}/activate`, and `PATCH /v1/installers/{id}` (from Plan A).
- Tests run in the backend image with the repo root mounted: `docker compose run --rm --no-deps -v "$PWD:/repo" -w /repo backend python -m pytest installer/test_release.py -v` (the container has httpx; `installer/` is not under `backend/`).
- `rollout=0` is a valid value (must NOT be dropped as falsy).

---

### Task 1: `installer/release.py` + `installer/test_release.py`

**Files:**
- Create: `installer/release.py`
- Test: `installer/test_release.py`

**Interfaces:**
- Produces: `derive_installer_type(platform, file_path) -> str`; `build_patch_payload(*, rollout, mandatory, channel, min_supported, expires) -> dict`; `publish(client, *, base_url, token, file_path, platform, version, channel, rollout, mandatory, min_supported, expires, activate=True) -> dict`.

- [ ] **Step 1: Write the failing test**

```python
# installer/test_release.py
import importlib.util, os, pathlib
spec = importlib.util.spec_from_file_location(
    "release", str(pathlib.Path(__file__).parent / "release.py"))
release = importlib.util.module_from_spec(spec); spec.loader.exec_module(release)


def test_derive_installer_type():
    assert release.derive_installer_type("macos", "X.dmg") == "dmg"
    assert release.derive_installer_type("windows", "X.msi") == "msi"
    assert release.derive_installer_type("windows", "X.exe") == "exe"


def test_build_patch_payload_keeps_zero_rollout():
    p = release.build_patch_payload(rollout=0, mandatory=True, channel="beta",
                                    min_supported=None, expires=None)
    assert p == {"rollout_percent": 0, "mandatory": True, "channel": "beta"}


class _Resp:
    def __init__(self, data=None): self._d = data or {}
    def json(self): return self._d
    def raise_for_status(self): pass


class _FakeClient:
    def __init__(self): self.calls = []
    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return _Resp({"id": "inst-123"} if url.endswith("/installers") else {})
    def patch(self, url, **kw):
        self.calls.append(("PATCH", url, kw)); return _Resp({})


def test_publish_orders_upload_patch_activate(tmp_path):
    f = tmp_path / "TimeTracker.dmg"; f.write_bytes(b"binary")
    client = _FakeClient()
    out = release.publish(
        client, base_url="https://api.test", token="tok", file_path=str(f),
        platform="macos", version="1.2.0", channel="stable", rollout=25,
        mandatory=False, min_supported=None, expires=None, activate=True)
    assert out["id"] == "inst-123" and out["installer_type"] == "dmg"
    seq = [(m, u.rsplit("/", 2)[-2] + "/" + u.rsplit("/", 1)[-1] if u.count("/") > 2 else u)
           for (m, u, _) in client.calls]
    methods = [m for (m, _, _) in client.calls]
    assert methods == ["POST", "PATCH", "POST"]            # upload, patch, activate
    assert client.calls[0][1].endswith("/v1/installers")
    assert client.calls[1][1].endswith("/v1/installers/inst-123")
    assert client.calls[2][1].endswith("/v1/installers/inst-123/activate")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose run --rm --no-deps -v "$PWD:/repo" -w /repo backend python -m pytest installer/test_release.py -v`
Expected: FAIL (`release.py` missing).

- [ ] **Step 3: Implement `installer/release.py`**

```python
#!/usr/bin/env python3
"""Publish a built agent installer to TimeTracker.

Runs AFTER the platform build scripts produce a .dmg/.msi. Orchestrates existing
backend endpoints (upload → patch distribution → activate). The signed manifest is
generated server-side by the PATCH endpoint, so no signing key is needed here.

  python installer/release.py --file dist/TimeTracker-1.2.0.dmg --platform macos \
    --version 1.2.0 --channel stable --rollout 25 [--mandatory] [--min-supported 1.0.0] \
    [--expires 2026-07-18T00:00:00Z] --api https://api.tracker.onedge.co --token <admin-jwt>
"""
from __future__ import annotations

import argparse
import os
import sys


def derive_installer_type(platform: str, file_path: str) -> str:
    p = platform.lower()
    if p == "macos":
        return "dmg"
    if p == "windows":
        return "exe" if file_path.lower().endswith(".exe") else "msi"
    raise ValueError(f"unsupported platform: {platform}")


def build_patch_payload(*, rollout, mandatory, channel, min_supported, expires) -> dict:
    payload: dict = {}
    if rollout is not None:
        payload["rollout_percent"] = rollout
    if mandatory is not None:
        payload["mandatory"] = mandatory
    if channel:
        payload["channel"] = channel
    if min_supported:
        payload["min_supported_version"] = min_supported
    if expires:
        payload["expires_at"] = expires
    return payload


def publish(client, *, base_url, token, file_path, platform, version, channel,
            rollout, mandatory, min_supported, expires, activate=True) -> dict:
    base = base_url.rstrip("/")
    itype = derive_installer_type(platform, file_path)
    headers = {"Authorization": f"Bearer {token}"}

    with open(file_path, "rb") as fh:
        r = client.post(
            f"{base}/v1/installers",
            data={"platform": platform, "installer_type": itype, "version": version},
            files={"file": (os.path.basename(file_path), fh)},
            headers=headers,
        )
    r.raise_for_status()
    iid = r.json()["id"]

    payload = build_patch_payload(rollout=rollout, mandatory=mandatory, channel=channel,
                                  min_supported=min_supported, expires=expires)
    if payload:
        client.patch(f"{base}/v1/installers/{iid}", json=payload, headers=headers).raise_for_status()

    if activate:
        client.post(f"{base}/v1/installers/{iid}/activate", headers=headers).raise_for_status()

    return {"id": iid, "version": version, "platform": platform, "installer_type": itype}


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Publish an agent installer release.")
    ap.add_argument("--file", required=True)
    ap.add_argument("--platform", required=True, choices=["macos", "windows"])
    ap.add_argument("--version", required=True)
    ap.add_argument("--channel", default="stable")
    ap.add_argument("--rollout", type=int, default=100)
    ap.add_argument("--mandatory", action="store_true")
    ap.add_argument("--min-supported", default=None)
    ap.add_argument("--expires", default=None)
    ap.add_argument("--api", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--no-activate", action="store_true")
    args = ap.parse_args(argv)

    import httpx
    with httpx.Client(timeout=600) as client:
        out = publish(
            client, base_url=args.api, token=args.token, file_path=args.file,
            platform=args.platform, version=args.version, channel=args.channel,
            rollout=args.rollout, mandatory=args.mandatory or None,
            min_supported=args.min_supported, expires=args.expires,
            activate=not args.no_activate,
        )
    print(f"Published {out['platform']} {out['version']} ({out['installer_type']}) "
          f"id={out['id']} rollout={args.rollout}% mandatory={args.mandatory} channel={args.channel}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose run --rm --no-deps -v "$PWD:/repo" -w /repo backend python -m pytest installer/test_release.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit** — SKIPPED (per stakeholder: implement + test, don't commit).

## Self-Review
- SP3 spec (§7): upload → manifest/sign → distribution → activate, local build preserved, no key in tool → Task 1 ✓ (sign happens server-side via PATCH).
- `rollout=0` retained (test asserts) ✓. Placeholder scan clean. Types match Plan A's PATCH payload keys (`rollout_percent`, `mandatory`, `channel`, `min_supported_version`, `expires_at`).
