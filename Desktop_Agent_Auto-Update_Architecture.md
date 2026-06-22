# Desktop Agent Auto-Update Architecture

## Overview
This document describes a production-grade auto-update architecture for a desktop agent installed on Windows and macOS devices.

The design focuses on:
* Incremental updates instead of re-downloading full installers
* Controlled release publishing from an admin dashboard
* Secure manifest-based update checks
* Download resumption and retry handling
* Atomic file replacement
* Rollback on failure
* Recovery after crashes, power loss, or incomplete updates
* Support for large fleets of installed agents

The MSI and DMG installers are used only for initial installation. All later updates are handled by the update pipeline described below.

---

## Core Principles
* The running agent must never overwrite itself directly.
* The updater must run as a separate process.
* All release metadata must be signed.
* All downloaded files must be verified before installation.
* Updates must be resumable.
* Failed updates must restore the previous working version.
* The agent must continue running the current version if update checks fail.
* Version checks must be lightweight and cache-friendly.
* Large fleet rollouts must be gradual and observable.

---

## High-Level Architecture

```text
┌────────────────────┐
│   Admin Dashboard  │
└─────────┬──────────┘
          │
          ▼
┌────────────────────┐
│  Release Service   │
└─────────┬──────────┘
          │
          ▼
┌────────────────────┐
│ Manifest Generator │
└─────────┬──────────┘
          │
          ▼
┌────────────────────┐
│ Object Storage     │
│ (S3 / R2 / Blob)   │
└─────────┬──────────┘
          │
          ▼
┌────────────────────┐
│ CDN Distribution   │
└─────────┬──────────┘
          │
          ▼
      Installed
        Agents
```

---

## Client-Side Components

### Launcher

A small bootstrap executable.

**Responsibilities:**
* Start the agent
* Detect incomplete updates
* Resume or rollback interrupted updates
* Recover from failed launches
* Keep startup logic separate from the main app

### Agent

The main application. The agent must not directly replace live binaries.

**Responsibilities:**
* Business logic
* Telemetry
* User activity handling
* Version checking
* Update coordination
* Update scheduling decisions

### Updater

A separate process used only for installation and replacement.

**Responsibilities:**
* Download changed files
* Resume interrupted downloads
* Verify checksums and signatures
* Stage update files
* Replace files atomically
* Perform rollback if startup validation fails
* Clean temporary state after success

---

## Release Workflow

```text
Developer Creates Release
        ↓
Build Generated
        ↓
Artifacts Signed
        ↓
Upload Release Files
        ↓
Generate Manifest
        ↓
Generate Manifest Signature
        ↓
Run QA / Validation
        ↓
Approve Release
        ↓
Publish Release
        ↓
Agents Detect Update
```

### Release Storage Structure

Each version is immutable after publication.

```text
releases/
├── 1.0.0/
│   ├── manifest.json
│   ├── manifest.sig
│   ├── windows/
│   │   ├── agent.exe
│   │   ├── core.dll
│   │   └── ui.dll
│   └── macos/
│       └── agent.app
└── 1.1.0/
    ├── manifest.json
    ├── manifest.sig
    ├── windows/
    │   ├── agent.exe
    │   ├── core.dll
    │   └── ui.dll
    └── macos/
        └── agent.app
```

---

## Version API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/version` | Return the latest published version, rollout rules, manifest location, mandatory update status, and cache metadata. |

### Example Response

```json
{
  "latestVersion": "1.1.0",
  "mandatory": false,
  "rolloutPercent": 25,
  "manifestUrl": "https://cdn.company.com/releases/1.1.0/manifest.json",
  "signatureUrl": "https://cdn.company.com/releases/1.1.0/manifest.sig",
  "minSupportedVersion": "1.0.0",
  "releaseChannel": "stable",
  "gracePeriodEndsAt": "2026-06-25T00:00:00Z"
}
```

### Version API Behavior

* Must be fast and lightweight
* Must be cacheable at the edge
* Must not download full release files
* Must tolerate high request volume
* Must support random jitter on clients to avoid thundering herd effects

---

## Manifest Format

### Example Manifest

```json
{
  "version": "1.1.0",
  "releaseDate": "2026-06-18T12:00:00Z",
  "channel": "stable",
  "mandatory": false,
  "expiresAt": "2026-07-01T00:00:00Z",
  "files": [
    {
      "path": "agent.exe",
      "sha256": "abc123",
      "size": 25123456
    },
    {
      "path": "core.dll",
      "sha256": "def456",
      "size": 5345678
    },
    {
      "path": "ui.dll",
      "sha256": "ghi789",
      "size": 9123456
    }
  ]
}
```

### Manifest Rules

* The manifest must be signed.
* The manifest must include version metadata, file hashes, and file sizes.
* The manifest should have an expiry time.
* The agent must reject stale manifests.
* The agent must reject older manifests than the newest trusted version already seen.

---

## End-to-End Update Flow

```text
Launcher Starts
        ↓
Agent Starts
        ↓
Read Current Version
        ↓
Add Random Jitter
        ↓
Call Version API
        ↓
Compare Versions
        ↓
Update Available?
├── No → Continue Normal Operation
└── Yes
      ↓
Download Manifest
      ↓
Verify Manifest Signature
      ↓
Check Manifest Expiry
      ↓
Compare Local Files
      ↓
Identify Changed Files
      ↓
Check Free Disk Space
      ↓
Download Changed Files
      ↓
Verify Each Download
      ↓
Stage Update
      ↓
Acquire Update Lock
      ↓
Launch Updater
      ↓
Stop Agent
      ↓
Atomically Replace Files
      ↓
Start Updated Agent
      ↓
Run Health Check
      ↓
Success?
├── Yes → Mark Update Complete
└── No  → Rollback Previous Version
```

---

## Network and Connectivity Edge Cases

### No Internet

If the device has no internet connection, it must continue normal operation, save a retry state, and retry using exponential backoff with random jitter. Example retry intervals: `5 min → 15 min → 30 min → 1 hr → 4 hr → 12 hr`.

### DNS Failure

Treat as a transient network failure. Continue running the current version and retry later.

### CDN Unavailable

Retry using the CDN path. Retry later if all paths fail. Never block the agent from running.

### Partial Download

Preserve downloaded bytes and resume with HTTP range requests. Use ETag or checksum-compatible resume metadata.

### Slow Network

Continue with resumed downloads. Avoid restarting from zero. Allow long download windows and keep the agent operational.

### Timeout

Return to normal application mode and retry later using backoff.

---

## Download Optimization Edge Cases

### Only Download Changed Files

The updater must compare the manifest against local file hashes and download only modified files.

### Large Files

Support resumable downloads, verify chunk integrity if chunking is used, and avoid repeated full downloads.

### Duplicate Downloads

Deduplicate by content hash and reuse the existing staged file if valid.

### Cached Files

If a file already exists in the cache and matches the expected hash, reuse it and skip re-download.

---

## Safety and Concurrency Edge Cases

### Update Lock

Only one update may run at a time. Use a file lock, OS mutex, or an update state marker.

### Multiple Agent Instances

If multiple processes try to update at once, allow only one updater instance while the remaining instances exit or wait.

### Agent Already Updating

Detect lock ownership to prevent duplicate update launches.

### Update While User Is Active

Stage the update in the background. Install when the process is idle or at the next restart, avoiding work interruptions unless the update is mandatory.

---

## Disk and Storage Edge Cases

### Insufficient Disk Space

Before downloading or staging, check available space. The updater must account for download size, temporary staging size, backup size, and replacement overhead. If space is insufficient, abort the update, keep the current version running, and retry later after space is available.

### Disk Full During Download

If disk space runs out mid-download, keep the staged partial state, free temporary files if possible, and retry after space becomes available.

### Corrupted Local Cache

If cached data is corrupted, delete the invalid cache items and re-fetch from the source.

---

## File Integrity Edge Cases

### Hash Mismatch

If a file hash does not match the manifest upon complete download, delete the file and retry the download.

### Truncated File

If the file size is smaller than expected, treat it as invalid and re-download.

### Tampered File

If signature or checksum validation fails, abort the installation and do not replace any active binary.

### Manifest Tampering / Replay Attack / Expired Manifest

Reject the update if the manifest signature fails, if it is older than the last trusted version, or if it is expired. Maintain the highest trusted version marker locally and keep the current version running.

---

## Atomic Replacement Edge Cases

### Power Loss During Install

If power is lost while replacing files, keep backups, record the update state before replacement, and recover on the next launch.

### Crash During Replace

If the process crashes mid-replacement, detect the incomplete update on the next start and restore the previous working version if needed.

### File In Use

If a file is locked by another process, delay replacement, stop dependent processes if safe, and retry the replacement.

### Partial Replacement

If only some files were replaced, treat the update as incomplete and restore from backups or staged files.

### Rename Failure

If the atomic rename fails, abort safely and restore the last known good version.

---

## Recovery and Resume Edge Cases

### Interrupted Update

If the device shuts down or restarts during an update, detect the incomplete update on start, read the state file, and resume or rollback.

### Update State File (Example)

```json
{
  "status": "DOWNLOADING",
  "version": "1.1.0",
  "progress": 45,
  "lastUpdated": "2026-06-18T12:30:00Z"
}
```

### Resume After Reboot

On startup, read the update state to determine whether the download was complete. Continue from the last safe checkpoint, or roll back if the update was partially applied.

### Retry After Failure

If an update fails, keep a failure record, retry after a delay, and do not loop infinitely.

---

## Rollback Edge Cases

### Health Check Failure

If the updated version fails startup validation, restore the previous binaries, start the previous version, and clear the incomplete state.

### Crash After Upgrade

If the app starts but crashes immediately, mark the update as unhealthy and roll back automatically.

### Rollback Failure

If the rollback itself fails, retry restoration from backup, leave a recovery marker for the launcher, and attempt a safe boot into the last working state.

### Rollback to Older Version

If a downgrade is allowed by policy, restore the previous supported version and keep the version history.

---

## Versioning Edge Cases

### Skipping Multiple Versions

The updater must allow a direct upgrade from an old version to the latest version without requiring every intermediate version to be installed.

### Mandatory Update

If a release is marked mandatory, the agent should not continue indefinitely on the old version. It must retry until the update succeeds or an approved fallback path is used.

### Minimum Supported Version

If the current version is below the minimum supported version, mark the update as required and prevent continued operation if policy requires it.

### Version Rollout Percentage

The release may be exposed gradually (e.g., 5% → 25% → 50% → 100%). The agent should only update if it is included in the current rollout bucket.

### Rollback Release

If a release is withdrawn, the Version API should stop serving it as the latest, and agents should move to the next approved version according to policy.

---

## Scheduling Edge Cases

### Random Jitter

To prevent update storms, add a random delay before version checks and spread checks over a window of time.

### Scheduled Maintenance Window

If installs should only happen at certain times, download early and install during the allowed window.

### Active User Session

If the user is active, defer non-mandatory restarts and wait for an idle state or the next launch.

### Offline Mode

If the device is offline for a long period, continue running the current version, retry later, and do not block normal function.

---

## Telemetry and Observability

Every stage should produce telemetry tracked per Device, Version, Platform, Release channel, Error type, and Retry count.

**Events to track:**

* `CHECK_STARTED`, `CHECK_SUCCESS`, `CHECK_FAILED`
* `MANIFEST_DOWNLOAD_STARTED`, `MANIFEST_DOWNLOAD_SUCCESS`, `MANIFEST_VERIFY_FAILED`
* `FILE_DOWNLOAD_STARTED`, `FILE_DOWNLOAD_RESUMED`, `FILE_DOWNLOAD_SUCCESS`, `FILE_DOWNLOAD_FAILED`
* `INSTALL_STARTED`, `INSTALL_SUCCESS`, `INSTALL_FAILED`
* `ROLLBACK_STARTED`, `ROLLBACK_SUCCESS`, `ROLLBACK_FAILED`

---

## State Management

The updater should maintain local state to resume interrupted downloads/installs, avoid duplicate work, track the last known good version, and record the current release channel.

**Example files:**
* `update_state.json`
* `update.lock`
* `manifest.cache`
* `last_successful_version.txt`

---

## Fleet and Scalability Edge Cases

### Thundering Herd

If many agents check at once, add jitter, cache Version API responses, and use a CDN for release files.

### Staged Rollout

The release can be published in controlled waves: Internal test group → Small production slice → Wider population → Full fleet.

### Multi-Region Distribution

Serve manifests and files through geographically close edges and keep the version API globally cached.

### Update Flood

If too many devices update at once, slow down rollout percentage increases, use backoff on retry, and limit concurrent downloads per device.

---

## Security Requirements

* All release metadata must be signed.
* All files must be hash verified.
* All transport must use HTTPS.
* The updater must trust only the embedded public key.
* The updater must reject unsigned or tampered content.
* Temporary files must not be executable until verified.
* Backups must be protected from accidental deletion until success is confirmed.

---

## Database Tables

### Releases

```sql
CREATE TABLE releases (
    id UUID PRIMARY KEY,
    version VARCHAR(50) NOT NULL,
    status VARCHAR(20) NOT NULL,
    mandatory BOOLEAN NOT NULL DEFAULT FALSE,
    rollout_percent INT NOT NULL DEFAULT 0,
    release_notes TEXT,
    created_by UUID,
    approved_by UUID,
    created_at TIMESTAMP NOT NULL,
    published_at TIMESTAMP
);
```

### Release Files

```sql
CREATE TABLE release_files (
    id UUID PRIMARY KEY,
    release_id UUID NOT NULL,
    platform VARCHAR(20) NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size BIGINT NOT NULL,
    storage_path TEXT NOT NULL
);
```

### Update Runs

```sql
CREATE TABLE update_runs (
    id UUID PRIMARY KEY,
    device_id UUID NOT NULL,
    current_version VARCHAR(50) NOT NULL,
    target_version VARCHAR(50) NOT NULL,
    status VARCHAR(50) NOT NULL,
    error_code VARCHAR(100),
    progress INT DEFAULT 0,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);
```

---

## Production Workflow Summary

```text
Developer Builds Release
        ↓
Artifacts Signed
        ↓
Upload to Dashboard
        ↓
QA Approval
        ↓
Publish Release
        ↓
Version API Updated
        ↓
Agents Check Version
        ↓
Random Jitter Added
        ↓
Manifest Downloaded
        ↓
Manifest Verified
        ↓
Local Files Compared
        ↓
Only Changed Files Downloaded
        ↓
Downloads Resumed If Interrupted
        ↓
Hashes Verified
        ↓
Update Staged
        ↓
Update Lock Acquired
        ↓
Updater Launched
        ↓
Agent Stops Safely
        ↓
Atomic File Replace
        ↓
Health Check
        ↓
Success?
├── Yes → Cleanup and Mark Complete
└── No  → Rollback to Last Known Good Version
```

---

## Final Notes

This architecture is designed to handle the most common and important production edge cases, including: no internet, DNS failure, CDN failure, partial downloads, disk full, corrupted downloads, file tampering, manifest replay, expired metadata, agent crashes, power loss, incomplete replacement, rollback failure, multiple concurrent update attempts, large-scale fleet rollout spikes, mandatory updates, skipped versions, and resume after reboot.

The result is a mature, resilient update system suitable for an internal enterprise desktop agent.
