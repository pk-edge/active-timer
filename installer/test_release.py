import importlib.util, pathlib
import pytest

spec = importlib.util.spec_from_file_location(
    "release", str(pathlib.Path(__file__).parent / "release.py"))
release = importlib.util.module_from_spec(spec); spec.loader.exec_module(release)


def test_derive_installer_type():
    assert release.derive_installer_type("macos", "X.dmg") == "dmg"
    assert release.derive_installer_type("windows", "X.msi") == "msi"
    assert release.derive_installer_type("windows", "X.exe") == "exe"


def test_derive_installer_type_unsupported():
    with pytest.raises(ValueError):
        release.derive_installer_type("linux", "X.bin")


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
    methods = [m for (m, _, _) in client.calls]
    assert methods == ["POST", "PATCH", "POST"]            # upload, patch, activate
    assert client.calls[0][1].endswith("/v1/installers")
    assert client.calls[1][1].endswith("/v1/installers/inst-123")
    assert client.calls[2][1].endswith("/v1/installers/inst-123/activate")
