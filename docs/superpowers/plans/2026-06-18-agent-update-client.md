# Agent Auto-Update — Client (SP4) Implementation Plan

> Execute task-by-task. Logic is unit-tested in the agent venv; the real
> binary-swap / relaunch / rollback is **device-tested by the stakeholder**
> (cannot run in the Docker backend env).

**Goal:** Upgrade the agent self-updater to the production design: jitter, signed-manifest + sha256 verification, resumable download with backoff + disk check, staged install behind a lock + state file, an updater **subprocess** (`--update`) that does atomic swap → relaunch → health check → rollback, telemetry reporting, and crash/power-loss recovery — all additively (only `updater.py` is rewritten; `main.py` gets one additive `--update` branch; nothing else changes).

**Architecture:** Split into a **pure testable core** `agent/update_verify.py` (no PySide6/OS mutation) + the **orchestrator/OS-action** `agent/updater.py`. Trust key lives in `agent/update_keys.json` (swappable per environment, like `backends.json`). Signature verify is import-guarded on `cryptography`; sha256 is always enforced; a missing/mismatched key falls back to sha256-only with a warning (never bricks updates).

**Tech Stack:** Python 3.13 agent venv, httpx, cryptography (new, additive), stdlib. Tests: `agent/.venv/bin/python -m pytest agent/tests/test_updater.py -v`.

## Global Constraints
- Additive: existing watchers/enrollment/identity/autostart/`main.py` startup unchanged; only `updater.py` internals + one additive `--update` branch in `main.py`.
- sha256 ALWAYS enforced before install. Signature verified only when a trust key is present + `cryptography` importable; else sha256-only + warning.
- `rollout`/decision logic is server-side; the agent just consumes `/check`.
- Never raise out of the worker — a broken update must not crash the agent.

---

### Task 1: Trust key file + `cryptography` dependency
**Files:** Create `agent/update_keys.json`; Modify `agent/requirements.txt`.
- `agent/update_keys.json`: `{"key_id": "v1", "public_key": "RqDArEHiUvyEc708TG5UKzv5QSLhutPc/7wQRUj7o4g="}` (DEV key, derived from the dev backend JWT_SECRET-signing key). **Production: replace with your real public key and set `AGENT_SIGNING_PRIVATE_KEY` on the backend.**
- `agent/requirements.txt`: add `cryptography==44.0.0` (ships wheels; bundled by PyInstaller).

### Task 2: `agent/update_verify.py` (pure, testable) + `agent/tests/test_updater.py`
**Interfaces (Produces):**
- `sha256_file(path) -> str`
- `parse_semver(s) -> tuple`; `is_newer(remote, local) -> bool`
- `load_trust_key(search_dirs) -> dict | None`
- `verify_signature(canonical_json, sig_b64, pubkey_b64) -> bool` (import-guarded; False if `cryptography` missing or bad)
- `verify_download(*, manifest, signature, file_path, trust_key) -> tuple[bool, str]` — sha256 of file must equal `manifest.files[0].sha256` (always); if `trust_key` + signature present, the canonical manifest signature must verify; returns `(ok, reason)`, never raises
- `read_state(path) -> dict`; `write_state(path, **fields)`; `clear_state(path)`
- `resume_offset(path) -> int`; `enough_disk_space(path, needed_bytes) -> bool`
- `report_payload(status, target_version, **kw) -> dict`

Tests cover: sha256 correctness; semver; trust-key load/missing; sha256 mismatch → not ok; signature good/tampered (skchipped if cryptography missing); state round-trip; resume offset; disk check; payload shape.

### Task 3: `updater.py` rewrite (orchestrator + OS actions) — device-tested
**Files:** Modify `agent/updater.py`; Modify `agent/main.py` (additive `--update` branch).
- `UpdateWorker.run`: jitter (random 0..N min) before each check loop.
- `_check_once`: call `/check`; if newer → fetch manifest+signature from response → resumable download (range + backoff `5m→15m→30m→1h→4h→12h`, disk check, resume from partial) → `verify_download` (sha256 + signature) → write `update_state=STAGED` + acquire `update.lock` → if user active and not mandatory, defer → else spawn `TimeTracker --update`.
- `run_update_install()` (invoked by the `--update` subprocess): backup current → atomic swap (macOS: stage app beside + `os.replace`/exchange into `/Applications`; Windows: `msiexec /i /quiet /norestart`) → relaunch → health check (process up within timeout) → on failure restore backup (rollback) → `clear_state` → telemetry at each stage via `POST /v1/updates/report`.
- `_recover_on_launch()`: read `update_state.json`; finish/rollback an interrupted install; resume a partial download.
- `main.py`: at the very top of `main()`, additively: `if "--update" in sys.argv: from updater import run_update_install; return run_update_install()` — normal launch path untouched.

**Device-test checklist (stakeholder, on macOS + Windows):** happy-path update; interrupted download resumes; sha256 mismatch aborts; mandatory installs without defer; kill mid-swap → rollback on next launch; offline → keeps running + retries.

## Self-Review
- SP4 spec (§8) items map to Tasks 1-3. sha256-always + verify-if-key (never brick) per stakeholder. Trust key in JSON file per stakeholder. `--update` subprocess (D7) not a separate launcher. Testable core split out so logic is unit-verified even though OS swap is device-tested.
