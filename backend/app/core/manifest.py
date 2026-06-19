"""Canonical (sorted, compact) update manifest + Ed25519 signature. Canonical
form is byte-stable so the signature is reproducible and verifiable by the agent."""
from __future__ import annotations

import json

from app.core import update_signing


def build_canonical(*, version, company_id, platform, installer_type, sha256, size,
                    channel, mandatory, min_supported_version, release_date,
                    expires_at, filename) -> str:
    doc = {
        "schema": 1,
        "version": version,
        "company_id": str(company_id),
        "platform": platform,
        "installer_type": installer_type,
        "channel": channel,
        "mandatory": bool(mandatory),
        "min_supported_version": min_supported_version,
        "release_date": release_date,
        "expires_at": expires_at,
        "files": [{"path": filename, "sha256": sha256, "size": size}],
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


def signed_manifest(**kwargs) -> tuple[str, str]:
    canonical = build_canonical(**kwargs)
    return canonical, update_signing.sign(canonical)
