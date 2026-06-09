"""
PolicyManager — manages the blocked-URL policy downloaded from the backend.

Responsibilities:
  1. Persist the policy list locally (JSON + SQLite cache).
  2. Apply network-level blocking by modifying the OS hosts file:
       - Windows: C:\\Windows\\System32\\drivers\\etc\\hosts  (agent runs as admin)
       - macOS:   /etc/hosts  (agent must have write permission via sudoers or installer)
  3. Provide is_blocked(url) for the URL watcher.

Hosts-file format added by this agent:
    # TimeTracker-begin
    127.0.0.1  blocked-domain.com
    127.0.0.1  www.blocked-domain.com
    # TimeTracker-end

On policy update the section is replaced atomically.
"""

import sys
import re
import json
import logging
import threading
from pathlib import Path

from config import Config

log = logging.getLogger("policy_manager")

if sys.platform == "win32":
    HOSTS_PATH = Path(r"C:\Windows\System32\drivers\etc\hosts")
else:
    HOSTS_PATH = Path("/etc/hosts")

MARKER_BEGIN = "# TimeTracker-begin"
MARKER_END   = "# TimeTracker-end"


class PolicyManager:
    def __init__(self, db):
        self._db      = db
        self._lock    = threading.Lock()
        self._patterns: list[str] = []
        # Load whatever is already cached in the local DB
        self._patterns = db.get_blocked_url_patterns()
        log.info("PolicyManager loaded %d blocked patterns from cache", len(self._patterns))

    # ── Public API ────────────────────────────────────────────────────────────

    def update(self, entries: list[dict]) -> None:
        """Apply a fresh policy list received from the backend."""
        patterns = [e["pattern"] for e in entries if e.get("pattern")]
        with self._lock:
            old_set = set(self._patterns)
            new_set = set(patterns)
            if old_set == new_set:
                return  # nothing changed
            self._patterns = patterns
            self._db.update_blocked_urls(entries)
            log.info("Policy updated: %d blocked patterns", len(patterns))
        self._apply_hosts(patterns)

    def is_blocked(self, url: str) -> bool:
        with self._lock:
            return _match_any(url, self._patterns)

    # ── Hosts-file management ─────────────────────────────────────────────────

    def _apply_hosts(self, patterns: list[str]) -> None:
        """Rewrite the TimeTracker section in the OS hosts file."""
        try:
            domains = _patterns_to_hosts_domains(patterns)
            _rewrite_hosts(domains)
            log.info("Hosts file updated (%d domains blocked)", len(domains))
        except PermissionError:
            log.warning(
                "Cannot write to hosts file — agent lacks write permission. "
                "URL blocking will only be logged, not enforced at OS level."
            )
        except Exception as e:
            log.error("Hosts file update failed: %s", e)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _match_any(url: str, patterns: list[str]) -> bool:
    try:
        from urllib.parse import urlparse
        host = re.sub(r"^www\.", "", (urlparse(url).hostname or "").lower())
    except Exception:
        host = url.lower()
    url_lower = url.lower()
    for p in patterns:
        p = p.lower().strip()
        if not p:
            continue
        if p == host or host.endswith("." + p) or p in url_lower:
            return True
    return False


def _patterns_to_hosts_domains(patterns: list[str]) -> list[str]:
    """Convert URL patterns to bare domain names for the hosts file."""
    domains: list[str] = []
    for p in patterns:
        p = p.strip().lower()
        if not p:
            continue
        # Strip scheme/path if present
        p = re.sub(r"^https?://", "", p)
        p = p.split("/")[0].split("?")[0]
        if p and "." in p:
            base = re.sub(r"^www\.", "", p)
            if base not in domains:
                domains.append(base)
    return domains


def _rewrite_hosts(domains: list[str]) -> None:
    """Atomically replace the TimeTracker section in the hosts file."""
    # Read existing content
    try:
        existing = HOSTS_PATH.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        existing = ""

    # Remove any previous TimeTracker block
    cleaned = re.sub(
        r"\n?" + re.escape(MARKER_BEGIN) + r".*?" + re.escape(MARKER_END) + r"\n?",
        "",
        existing,
        flags=re.DOTALL,
    )

    if domains:
        lines = [MARKER_BEGIN]
        for d in domains:
            lines.append(f"127.0.0.1  {d}")
            lines.append(f"127.0.0.1  www.{d}")
        lines.append(MARKER_END)
        block = "\n".join(lines) + "\n"
        new_content = cleaned.rstrip("\n") + "\n" + block
    else:
        new_content = cleaned

    # Write atomically: write to .tmp then replace
    tmp = HOSTS_PATH.with_suffix(".tmp_tt")
    try:
        tmp.write_text(new_content, encoding="utf-8")
        tmp.replace(HOSTS_PATH)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        # Fallback: direct write
        HOSTS_PATH.write_text(new_content, encoding="utf-8")

    # Flush DNS cache so the new block takes effect immediately
    _flush_dns()


def _flush_dns() -> None:
    """Flush the OS DNS cache after modifying the hosts file."""
    import subprocess
    try:
        if sys.platform == "win32":
            subprocess.run(["ipconfig", "/flushdns"], capture_output=True, timeout=10)
        elif sys.platform == "darwin":
            subprocess.run(
                ["dscacheutil", "-flushcache"],
                capture_output=True, timeout=10,
            )
            subprocess.run(
                ["killall", "-HUP", "mDNSResponder"],
                capture_output=True, timeout=10,
            )
        else:
            subprocess.run(
                ["systemd-resolve", "--flush-caches"],
                capture_output=True, timeout=10,
            )
    except Exception as e:
        log.debug("DNS flush failed (non-critical): %s", e)
