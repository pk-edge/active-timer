"""Unit tests for the agent updater's pure logic (agent/update_verify.py).
The real binary swap / relaunch / rollback is device-tested, not here."""
import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import update_verify as uv


def _manifest(sha):
    return {"schema": 1, "version": "1.2.0", "files": [{"path": "TimeTracker.dmg",
            "sha256": sha, "size": 6}]}


def test_sha256_and_semver(tmp_path):
    f = tmp_path / "x.bin"; f.write_bytes(b"binary")
    import hashlib
    assert uv.sha256_file(str(f)) == hashlib.sha256(b"binary").hexdigest()
    assert uv.is_newer("1.2.0", "1.1.9") is True
    assert uv.is_newer("1.2.0", "1.2.0") is False


def test_load_trust_key(tmp_path):
    (tmp_path / "update_keys.json").write_text(json.dumps({"key_id": "v1", "public_key": "abc"}))
    k = uv.load_trust_key([str(tmp_path)])
    assert k == {"key_id": "v1", "public_key": "abc"}
    assert uv.load_trust_key([str(tmp_path / "nope")]) is None


def test_verify_download_sha256(tmp_path):
    f = tmp_path / "TimeTracker.dmg"; f.write_bytes(b"binary")
    good = uv.sha256_file(str(f))
    ok, why = uv.verify_download(manifest=_manifest(good), signature=None,
                                 file_path=str(f), trust_key=None)
    assert ok is True and "sha256 only" in why
    ok, why = uv.verify_download(manifest=_manifest("00bad"), signature=None,
                                 file_path=str(f), trust_key=None)
    assert ok is False and "mismatch" in why


def test_verify_download_signature_roundtrip_and_tamper(tmp_path):
    crypto = pytest.importorskip("cryptography")  # skip if not installed
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    f = tmp_path / "TimeTracker.dmg"; f.write_bytes(b"binary")
    manifest = _manifest(uv.sha256_file(str(f)))
    priv = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)).decode()
    sig = base64.b64encode(priv.sign(uv.canonical_manifest(manifest).encode())).decode()
    trust = {"key_id": "v1", "public_key": pub_b64}

    ok, why = uv.verify_download(manifest=manifest, signature=sig, file_path=str(f), trust_key=trust)
    assert ok is True and "signature" in why

    # Wrong key → never-brick falls back to sha256 (ok True); strict → reject.
    other = base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)).decode()
    ok, why = uv.verify_download(manifest=manifest, signature=sig, file_path=str(f),
                                 trust_key={"key_id": "v1", "public_key": other})
    assert ok is True and "MISMATCH" in why
    ok, why = uv.verify_download(manifest=manifest, signature=sig, file_path=str(f),
                                 trust_key={"key_id": "v1", "public_key": other}, strict=True)
    assert ok is False


def test_state_resume_disk_payload(tmp_path):
    sp = str(tmp_path / "update_state.json")
    uv.write_state(sp, status="DOWNLOADING", version="1.2.0", progress=45)
    assert uv.read_state(sp)["progress"] == 45
    uv.write_state(sp, status="STAGED")
    st = uv.read_state(sp)
    assert st["status"] == "STAGED" and st["version"] == "1.2.0"   # merge, not overwrite
    uv.clear_state(sp); assert uv.read_state(sp) == {}

    part = tmp_path / "p.part"; part.write_bytes(b"12345")
    assert uv.resume_offset(str(part)) == 5
    assert uv.resume_offset(str(tmp_path / "absent")) == 0
    assert uv.enough_disk_space(str(tmp_path / "x"), 1) is True

    p = uv.report_payload("INSTALL_SUCCESS", "1.2.0", from_version="1.0.0", error_code=None)
    assert p == {"status": "INSTALL_SUCCESS", "target_version": "1.2.0", "from_version": "1.0.0"}


def test_verify_download_rejects_missing_files(tmp_path):
    f = tmp_path / "x"; f.write_bytes(b"x")
    ok, why = uv.verify_download(manifest={"version": "1.2.0"}, signature=None,
                                 file_path=str(f), trust_key=None)
    assert ok is False and "files" in why


def test_enough_disk_space_false_when_request_exceeds_free(tmp_path):
    # 1 exabyte will exceed any real disk → False; a tiny request → True.
    assert uv.enough_disk_space(str(tmp_path / "x"), 10 ** 18) is False
    assert uv.enough_disk_space(str(tmp_path / "x"), 1) is True
