import app.core.release_urls as ru


def test_cdn_branch(monkeypatch):
    monkeypatch.setattr(ru.settings, "CDN_BASE_URL", "https://cdn.example.com", raising=False)
    assert ru.download_url("installers/co/macos/1.2.0/TimeTracker.dmg") == \
        "https://cdn.example.com/installers/co/macos/1.2.0/TimeTracker.dmg"


def test_presigned_branch(monkeypatch):
    monkeypatch.setattr(ru.settings, "CDN_BASE_URL", "", raising=False)
    monkeypatch.setattr(ru, "_presign", lambda key: f"https://s3/{key}?sig=abc")
    assert ru.download_url("installers/x.dmg") == "https://s3/installers/x.dmg?sig=abc"
