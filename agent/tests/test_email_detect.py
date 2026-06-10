import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import email_detect


def _write_local_state(path: Path, info_cache: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"profile": {"info_cache": info_cache}}), encoding="utf-8")
    return path


def test_default_profile_email_wins(tmp_path, monkeypatch):
    state = _write_local_state(tmp_path / "Local State", {
        "Profile 1": {"user_name": "second@corp.com"},
        "Default":   {"user_name": "First@Corp.com"},
    })
    monkeypatch.setattr(email_detect, "_local_state_paths", lambda: [state])
    assert email_detect.detect_first_signed_in_email() == "first@corp.com"


def test_profiles_without_email_are_skipped(tmp_path, monkeypatch):
    state = _write_local_state(tmp_path / "Local State", {
        "Default":   {"user_name": ""},
        "Profile 1": {"gaia_name": "No Email"},
        "Profile 2": {"user_name": "real@corp.com"},
    })
    monkeypatch.setattr(email_detect, "_local_state_paths", lambda: [state])
    assert email_detect.detect_first_signed_in_email() == "real@corp.com"


def test_second_browser_used_when_first_empty(tmp_path, monkeypatch):
    chrome = _write_local_state(tmp_path / "chrome" / "Local State", {})
    edge = _write_local_state(tmp_path / "edge" / "Local State", {
        "Default": {"user_name": "edge.user@corp.com"},
    })
    monkeypatch.setattr(email_detect, "_local_state_paths", lambda: [chrome, edge])
    assert email_detect.detect_first_signed_in_email() == "edge.user@corp.com"


def test_missing_or_corrupt_files_return_none(tmp_path, monkeypatch):
    corrupt = tmp_path / "Local State"
    corrupt.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(email_detect, "_local_state_paths",
                        lambda: [tmp_path / "missing" / "Local State", corrupt])
    assert email_detect.detect_first_signed_in_email() is None
