"""Pure, testable helpers for the agent self-updater.

No PySide6, no OS mutation, no network — so this is unit-testable on any platform.
Security model (stakeholder choice "never brick"):
  - sha256 of the downloaded file is ALWAYS enforced against the manifest.
  - The Ed25519 manifest signature is verified WHEN a trust key is present and
    `cryptography` is importable. In the default (non-strict) mode a missing or
    mismatched signature falls back to sha256-only with a loud warning, so a key
    misconfiguration can never block the whole fleet from updating. Pass
    strict=True to refuse installation unless the signature verifies.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("update_verify")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_semver(s: str | None) -> tuple:
    parts = []
    for p in (s or "").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts) or (0,)


def is_newer(remote: str | None, local: str | None) -> bool:
    return parse_semver(remote) > parse_semver(local)


def load_trust_key(search_dirs) -> dict | None:
    """Return {'key_id','public_key'} from the first update_keys.json found, else None."""
    for d in search_dirs:
        if not d:
            continue
        p = Path(d) / "update_keys.json"
        try:
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8-sig"))
                if data.get("public_key"):
                    return {"key_id": data.get("key_id", "v1"), "public_key": data["public_key"]}
        except Exception as e:  # noqa: BLE001
            log.debug("trust key load failed at %s: %s", p, e)
    return None


def verify_signature(canonical_json: str, sig_b64: str, pubkey_b64: str) -> bool:
    """True iff `sig_b64` is a valid Ed25519 signature of `canonical_json` under
    `pubkey_b64`. Returns False (never raises) if cryptography is unavailable."""
    try:
        import base64
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pubkey_b64))
        pub.verify(base64.b64decode(sig_b64), canonical_json.encode("utf-8"))
        return True
    except Exception as e:  # noqa: BLE001
        log.debug("signature verify failed/unavailable: %s", e)
        return False


def canonical_manifest(manifest: dict) -> str:
    """Byte-stable form the backend signed (json sorted keys, compact separators)."""
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"))


def verify_download(*, manifest: dict, signature: str | None, file_path: str,
                    trust_key: dict | None, strict: bool = False) -> tuple[bool, str]:
    """Validate a downloaded installer. sha256 is mandatory; signature is verified
    when a trust key + signature are present. Returns (ok, reason); never raises."""
    files = (manifest or {}).get("files") or []
    if not files or not files[0].get("sha256"):
        return False, "manifest missing files[].sha256"
    expected = files[0]["sha256"]
    actual = sha256_file(file_path)
    if actual.lower() != expected.lower():
        return False, f"sha256 mismatch ({actual} != {expected})"

    if trust_key and signature:
        if verify_signature(canonical_manifest(manifest), signature, trust_key["public_key"]):
            return True, "verified: sha256 + signature"
        if strict:
            return False, "signature verification failed (strict)"
        log.warning("update manifest signature did NOT verify — proceeding on sha256 "
                    "only (never-brick mode). Check that update_keys.json matches the "
                    "backend signing key.")
        return True, "sha256 ok; signature MISMATCH (fell back, never-brick)"

    return True, "verified: sha256 only (no trust key/signature)"


# ── update_state.json (resume / recovery) ────────────────────────────────────

def read_state(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except Exception:  # noqa: BLE001
        return {}


def write_state(path: str, **fields) -> None:
    data = read_state(path)
    data.update(fields)
    Path(path).write_text(json.dumps(data))


def clear_state(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def resume_offset(path: str) -> int:
    """Bytes already downloaded for a partial file (0 if absent)."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def enough_disk_space(path: str, needed_bytes: int) -> bool:
    """True if `path`'s filesystem has at least `needed_bytes` free. Unknown → True
    (don't block on an inability to measure)."""
    try:
        import shutil
        free = shutil.disk_usage(os.path.dirname(path) or ".").free
        return free >= needed_bytes
    except Exception:  # noqa: BLE001
        return True


def report_payload(status: str, target_version: str, **kw) -> dict:
    """Body for POST /v1/updates/report — drops None values."""
    p = {"status": status, "target_version": target_version}
    p.update({k: v for k, v in kw.items() if v is not None})
    return p
