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
