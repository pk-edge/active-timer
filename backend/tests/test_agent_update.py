import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sa_text

from app.core.security import create_device_token, create_admin_token
from app.core import update_signing


async def _seed_device_and_installer(db, **installer_overrides):
    """Create a throwaway company + active device + active installer in the
    rolled-back test session. Returns (device_headers, company_id, version).
    The device's jwt_token is set to the minted token because get_current_device
    matches Device.jwt_token == presented credential."""
    cid = uuid.uuid4(); did = uuid.uuid4(); tag = uuid.uuid4().hex[:8]
    token = create_device_token(str(did), str(cid))
    await db.execute(sa_text(
        "INSERT INTO companies (id, name, enrollment_token) VALUES (:i,:n,:t)"),
        {"i": str(cid), "n": f"UP-{tag}", "t": tag})
    await db.execute(sa_text(
        "INSERT INTO devices (id, company_id, hostname, platform, agent_version, jwt_token, is_active) "
        "VALUES (:i,:c,:h,'darwin','1.0.0',:tok,TRUE)"),
        {"i": str(did), "c": str(cid), "h": f"h-{tag}", "tok": token})
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
    headers = {"Authorization": f"Bearer {token}"}
    return headers, cid, fields["version"]


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


# ── Task 7: enhanced /v1/updates/check ────────────────────────────────────────

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


@pytest.mark.asyncio
async def test_check_below_min_version_forces_even_at_zero_rollout(client, db_session, monkeypatch):
    # Device is 1.0.0; min_supported_version 2.0.0 → forced update despite 0% rollout.
    monkeypatch.setattr("app.core.release_urls.settings.CDN_BASE_URL", "https://cdn.test", raising=False)
    headers, _, _ = await _seed_device_and_installer(db_session, rollout_percent=0, min_supported_version="2.0.0")
    assert (await client.get("/v1/updates/check", headers=headers)).json()["update_available"] is True


@pytest.mark.asyncio
async def test_check_same_version_no_update(client, db_session, monkeypatch):
    # Active version equals the device's reported version → not newer → no update.
    monkeypatch.setattr("app.core.release_urls.settings.CDN_BASE_URL", "https://cdn.test", raising=False)
    headers, _, _ = await _seed_device_and_installer(db_session, version="1.0.0")
    assert (await client.get("/v1/updates/check", headers=headers)).json()["update_available"] is False


@pytest.mark.asyncio
async def test_check_no_active_installer(client, db_session):
    cid = uuid.uuid4(); did = uuid.uuid4(); tag = uuid.uuid4().hex[:8]
    token = create_device_token(str(did), str(cid))
    await db_session.execute(sa_text(
        "INSERT INTO companies (id, name, enrollment_token) VALUES (:i,:n,:t)"),
        {"i": str(cid), "n": f"NA-{tag}", "t": tag})
    await db_session.execute(sa_text(
        "INSERT INTO devices (id, company_id, hostname, platform, agent_version, jwt_token, is_active) "
        "VALUES (:i,:c,:h,'darwin','1.0.0',:tok,TRUE)"),
        {"i": str(did), "c": str(cid), "h": f"h-{tag}", "tok": token})
    await db_session.flush()
    r = await client.get("/v1/updates/check", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["update_available"] is False


# ── Update-on-change: uploading a changed DMG/MSI is detected ─────────────────

@pytest.mark.asyncio
async def test_update_detected_when_macos_dmg_changes(client, db_session, monkeypatch):
    monkeypatch.setattr("app.core.release_urls.settings.CDN_BASE_URL", "https://cdn.test", raising=False)
    # Active build equals the device's version → no update offered yet.
    headers, cid, _ = await _seed_device_and_installer(db_session, version="1.0.0")
    assert (await client.get("/v1/updates/check", headers=headers)).json()["update_available"] is False

    # Publish a CHANGED macOS build: new version + new sha256, activated.
    new_sha = "a" * 64
    await db_session.execute(sa_text(
        "UPDATE installer_versions SET is_active=FALSE WHERE company_id=:c"), {"c": str(cid)})
    await db_session.execute(sa_text(
        "INSERT INTO installer_versions (id,company_id,platform,installer_type,version,s3_key,"
        "file_size,sha256,is_active,rollout_percent,mandatory,channel) VALUES "
        "(:id,:c,'macos','dmg','1.3.0',:k,999,:sha,TRUE,100,FALSE,'stable')"),
        {"id": str(uuid.uuid4()), "c": str(cid),
         "k": f"installers/{cid}/macos/1.3.0/TimeTracker.dmg", "sha": new_sha})
    await db_session.flush()

    body = (await client.get("/v1/updates/check", headers=headers)).json()
    assert body["update_available"] is True
    assert body["version"] == "1.3.0" and body["installer_type"] == "dmg"
    assert body["sha256"] == new_sha                              # agent sees the new hash
    assert body["manifest"]["files"][0]["sha256"] == new_sha      # signed manifest reflects it


@pytest.mark.asyncio
async def test_update_detected_when_windows_msi_changes(client, db_session, monkeypatch):
    monkeypatch.setattr("app.core.release_urls.settings.CDN_BASE_URL", "https://cdn.test", raising=False)
    cid = uuid.uuid4(); did = uuid.uuid4(); tag = uuid.uuid4().hex[:8]
    token = create_device_token(str(did), str(cid))
    await db_session.execute(sa_text(
        "INSERT INTO companies (id, name, enrollment_token) VALUES (:i,:n,:t)"),
        {"i": str(cid), "n": f"WIN-{tag}", "t": tag})
    await db_session.execute(sa_text(
        "INSERT INTO devices (id, company_id, hostname, platform, agent_version, jwt_token, is_active) "
        "VALUES (:i,:c,:h,'win32','1.0.0',:tok,TRUE)"),
        {"i": str(did), "c": str(cid), "h": f"w-{tag}", "tok": token})
    new_sha = "b" * 64
    await db_session.execute(sa_text(
        "INSERT INTO installer_versions (id,company_id,platform,installer_type,version,s3_key,"
        "file_size,sha256,is_active,rollout_percent,mandatory,channel) VALUES "
        "(:id,:c,'windows','msi','2.0.0',:k,999,:sha,TRUE,100,FALSE,'stable')"),
        {"id": str(uuid.uuid4()), "c": str(cid),
         "k": f"installers/{cid}/windows/2.0.0/TimeTracker.msi", "sha": new_sha})
    await db_session.flush()

    body = (await client.get("/v1/updates/check", headers={"Authorization": f"Bearer {token}"})).json()
    assert body["update_available"] is True
    assert body["version"] == "2.0.0" and body["installer_type"] == "msi"
    assert body["sha256"] == new_sha


# ── Task 8: GET /v1/updates/manifest/{id} ─────────────────────────────────────

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


# ── Task 9: telemetry report + runs ───────────────────────────────────────────

async def _seed_admin(db, company_id):
    """Insert a minimal admin employee (role='admin' → legacy all-perms fallback)
    and return admin auth headers."""
    eid = uuid.uuid4(); tag = uuid.uuid4().hex[:8]
    await db.execute(sa_text(
        "INSERT INTO employees (id, company_id, email, name, role, is_active) "
        "VALUES (:i,:c,:e,:n,'admin',TRUE)"),
        {"i": str(eid), "c": str(company_id), "e": f"admin-{tag}@example.com", "n": "Admin"})
    await db.flush()
    return {"Authorization": f"Bearer {create_admin_token(str(eid), 'admin', str(company_id))}"}


async def _cleanup_company(db, company_id):
    """Delete a seeded company + its rows in FK order, then commit. The report/patch
    endpoints under test call db.commit(), which persists seed data on the shared
    session; this keeps the dev DB clean (mirrors the e2e tag-and-cleanup pattern)."""
    c = str(company_id)
    try:
        for table in ("update_runs", "installer_versions", "app_category_rules",
                      "app_categories", "devices", "employees"):
            await db.execute(sa_text(f"DELETE FROM {table} WHERE company_id = :c"), {"c": c})
        await db.execute(sa_text("DELETE FROM companies WHERE id = :c"), {"c": c})
        await db.commit()
    except Exception:
        # Best-effort: a residual FK (e.g. audit rows) must not fail the test after
        # its assertions already passed. Leftover rows are uniquely tagged + harmless.
        await db.rollback()


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
    await _cleanup_company(db_session, cid)


@pytest.mark.asyncio
async def test_runs_read_is_company_scoped(client, db_session):
    headers, cid, ver = await _seed_device_and_installer(db_session, rollout_percent=100)
    await client.post("/v1/updates/report", headers=headers,
                      json={"target_version": ver, "status": "INSTALL_SUCCESS"})
    admin_h = await _seed_admin(db_session, cid)
    r = await client.get("/v1/updates/runs", headers=admin_h)
    assert r.status_code == 200
    assert any(run["target_version"] == ver for run in r.json()["runs"])
    await _cleanup_company(db_session, cid)


# ── Task 10: PATCH /v1/installers/{id} distribution ───────────────────────────

@pytest.mark.asyncio
async def test_patch_distribution_sets_fields_and_manifest(client, db_session):
    headers, cid, ver = await _seed_device_and_installer(db_session, rollout_percent=100)
    row = await db_session.execute(sa_text(
        "SELECT id FROM installer_versions WHERE company_id = :c"), {"c": str(cid)})
    iid = str(row.scalar_one())
    admin_h = await _seed_admin(db_session, cid)
    r = await client.patch(f"/v1/installers/{iid}", headers=admin_h,
                           json={"rollout_percent": 25, "mandatory": True, "channel": "beta"})
    assert r.status_code == 200
    saved = await db_session.execute(sa_text(
        "SELECT rollout_percent, mandatory, channel, manifest_json, manifest_sig "
        "FROM installer_versions WHERE id = :i"), {"i": iid})
    s = saved.first()
    assert s.rollout_percent == 25 and s.mandatory is True and s.channel == "beta"
    assert s.manifest_json and s.manifest_sig    # manifest regenerated + signed
    await _cleanup_company(db_session, cid)


# ── Chrome normalization: category rules stored canonical ─────────────────────

@pytest.mark.asyncio
async def test_category_rule_stored_canonical(client, db_session):
    cid = uuid.uuid4(); tag = uuid.uuid4().hex[:8]
    await db_session.execute(sa_text(
        "INSERT INTO companies (id, name, enrollment_token) VALUES (:i,:n,:t)"),
        {"i": str(cid), "n": f"NR-{tag}", "t": tag})
    await db_session.flush()
    admin_h = await _seed_admin(db_session, cid)

    r = await client.post("/v1/tech/categories", headers=admin_h,
                          json={"name": f"Tools-{tag}", "color": "#123456"})
    assert r.status_code == 201, r.text
    cat_id = r.json()["id"]

    # A rule created with a raw process name is stored canonical (powershell is
    # normalized to PowerShell and is not one of the default-seeded patterns).
    r = await client.post(f"/v1/tech/categories/{cat_id}/rules", headers=admin_h,
                          json={"app_pattern": "powershell"})
    assert r.status_code == 201, r.text
    assert r.json()["app_pattern"] == "PowerShell"

    # /apps/detail now filters normalize_app_name(ae.app_name) = :app_name — must not error.
    r = await client.get("/v1/tech/apps/detail?app_name=Google%20Chrome&days=7", headers=admin_h)
    assert r.status_code == 200, r.text

    await _cleanup_company(db_session, cid)


# ── +1h-on-restart fix: device-today-active honors the device's local midnight ─

@pytest.mark.asyncio
async def test_device_today_active_respects_local_midnight_since(client, db_session):
    cid = uuid.uuid4(); did = uuid.uuid4(); tag = uuid.uuid4().hex[:8]
    token = create_device_token(str(did), str(cid))
    await db_session.execute(sa_text(
        "INSERT INTO companies (id, name, enrollment_token) VALUES (:i,:n,:t)"),
        {"i": str(cid), "n": f"TZ-{tag}", "t": tag})
    await db_session.execute(sa_text(
        "INSERT INTO devices (id, company_id, hostname, platform, jwt_token, is_active) "
        "VALUES (:i,:c,:h,'darwin',:tok,TRUE)"),
        {"i": str(did), "c": str(cid), "h": f"h-{tag}", "tok": token})

    since = 1_700_000_000  # device's local midnight (absolute unix)
    # 600s of activity 1h BEFORE local midnight (yesterday — must be EXCLUDED),
    # 900s 1min after (today — must be INCLUDED). Excluding the pre-midnight
    # block is exactly what stops the '+1h on restart' inflation.
    for off, dur in ((-3600, 600), (60, 900)):
        await db_session.execute(sa_text(
            "INSERT INTO activity_events (id, device_id, event_type, app_name, duration, timestamp) "
            "VALUES (:i,:d,'window_change','Code',:dur, to_timestamp(:ts))"),
            {"i": str(uuid.uuid4()), "d": str(did), "dur": dur, "ts": since + off})
    await db_session.flush()

    headers = {"Authorization": f"Bearer {token}"}
    r = await client.get(f"/v1/home/device-today-active?since={since}", headers=headers)
    assert r.status_code == 200
    assert r.json()["active_seconds_today"] == 900   # only the post-midnight block

    # No `since` (older agent) still works (UTC-day fallback, no error).
    r2 = await client.get("/v1/home/device-today-active", headers=headers)
    assert r2.status_code == 200


# ── Meeting logs: session aggregation (active time + apps-used) ───────────────

@pytest.mark.asyncio
async def test_meetings_endpoint_aggregates_sessions(client, db_session):
    cid = uuid.uuid4(); did = uuid.uuid4(); eid = uuid.uuid4(); tag = uuid.uuid4().hex[:8]
    await db_session.execute(sa_text(
        "INSERT INTO companies (id, name, enrollment_token) VALUES (:i,:n,:t)"),
        {"i": str(cid), "n": f"MTG-{tag}", "t": tag})
    admin_h = await _seed_admin(db_session, cid)
    await db_session.execute(sa_text(
        "INSERT INTO employees (id, company_id, email, name, role, is_active) "
        "VALUES (:i,:c,:e,:n,'member',TRUE)"),
        {"i": str(eid), "c": str(cid), "e": f"u-{tag}@x.com", "n": "Worker"})
    await db_session.execute(sa_text(
        "INSERT INTO devices (id, company_id, employee_id, hostname, platform, is_active) "
        "VALUES (:i,:c,:emp,:h,'darwin',TRUE)"),
        {"i": str(did), "c": str(cid), "emp": str(eid), "h": f"h-{tag}"})

    def ev(etype, app, mins_ago, dur=0, title=""):
        return {"i": str(uuid.uuid4()), "d": str(did), "t": etype, "a": app,
                "ti": title, "dur": dur, "m": mins_ago}

    rows = [
        # Real Zoom meeting [120m ago, 100m ago]: 600s Zoom + 300s Chrome inside;
        # 999s Slack BEFORE the meeting must be excluded.
        ev("meeting_start", "Zoom", 120, 0, "Standup"),
        ev("window_change", "Zoom", 119, 600),
        ev("window_change", "Google Chrome", 110, 300),
        ev("meeting_end", "Zoom", 100, 1200),
        ev("window_change", "Slack", 150, 999),
        # Trivial meeting (10s active) → dropped as noise.
        ev("meeting_start", "Webex", 80, 0),
        ev("window_change", "Webex", 79, 10),
        ev("meeting_end", "Webex", 75, 300),
    ]
    for r in rows:
        await db_session.execute(sa_text(
            "INSERT INTO activity_events "
            "(id, device_id, event_type, app_name, window_title, duration, timestamp) "
            "VALUES (:i,:d,:t,:a,:ti,:dur, NOW() - make_interval(mins => :m))"), r)
    await db_session.flush()

    body = (await client.get(f"/v1/home/meetings?device_id={did}&days=2", headers=admin_h)).json()
    assert body["count"] == 1                    # trivial Webex meeting filtered out
    assert body["total_active_secs"] == 900      # 600 Zoom + 300 Chrome (Slack excluded)
    s = body["sessions"][0]
    assert s["platform"] == "Zoom"
    assert {a["app_name"]: a["seconds"] for a in s["apps"]} == {"Zoom": 600, "Google Chrome": 300}

    # Same result via the per-user filter (the dashboard user view).
    by_emp = (await client.get(f"/v1/home/meetings?employee_id={eid}&days=2", headers=admin_h)).json()
    assert by_emp["count"] == 1 and by_emp["total_active_secs"] == 900
