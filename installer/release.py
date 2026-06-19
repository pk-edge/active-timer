#!/usr/bin/env python3
"""Publish a built agent installer to TimeTracker.

Runs AFTER the platform build scripts produce a .dmg/.msi. Orchestrates existing
backend endpoints (upload -> patch distribution -> activate). The signed manifest is
generated server-side by the PATCH endpoint, so no signing key is needed here.

  python installer/release.py --file dist/TimeTracker-1.2.0.dmg --platform macos \
    --version 1.2.0 --channel stable --rollout 25 [--mandatory] [--min-supported 1.0.0] \
    [--expires 2026-07-18T00:00:00Z] --api https://api.tracker.onedge.co --token <admin-jwt>
"""
from __future__ import annotations

import argparse
import os
import sys


def derive_installer_type(platform: str, file_path: str) -> str:
    p = platform.lower()
    if p == "macos":
        return "dmg"
    if p == "windows":
        return "exe" if file_path.lower().endswith(".exe") else "msi"
    raise ValueError(f"unsupported platform: {platform}")


def build_patch_payload(*, rollout, mandatory, channel, min_supported, expires) -> dict:
    payload: dict = {}
    if rollout is not None:
        payload["rollout_percent"] = rollout
    if mandatory is not None:
        payload["mandatory"] = mandatory
    if channel:
        payload["channel"] = channel
    if min_supported:
        payload["min_supported_version"] = min_supported
    if expires:
        payload["expires_at"] = expires
    return payload


def publish(client, *, base_url, token, file_path, platform, version, channel,
            rollout, mandatory, min_supported, expires, activate=True) -> dict:
    base = base_url.rstrip("/")
    itype = derive_installer_type(platform, file_path)
    headers = {"Authorization": f"Bearer {token}"}

    with open(file_path, "rb") as fh:
        r = client.post(
            f"{base}/v1/installers",
            data={"platform": platform, "installer_type": itype, "version": version},
            files={"file": (os.path.basename(file_path), fh)},
            headers=headers,
        )
    r.raise_for_status()
    iid = r.json()["id"]

    payload = build_patch_payload(rollout=rollout, mandatory=mandatory, channel=channel,
                                  min_supported=min_supported, expires=expires)
    if payload:
        client.patch(f"{base}/v1/installers/{iid}", json=payload, headers=headers).raise_for_status()

    if activate:
        client.post(f"{base}/v1/installers/{iid}/activate", headers=headers).raise_for_status()

    return {"id": iid, "version": version, "platform": platform, "installer_type": itype}


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Publish an agent installer release.")
    ap.add_argument("--file", required=True)
    ap.add_argument("--platform", required=True, choices=["macos", "windows"])
    ap.add_argument("--version", required=True)
    ap.add_argument("--channel", default="stable")
    ap.add_argument("--rollout", type=int, default=100)
    ap.add_argument("--mandatory", action="store_true")
    ap.add_argument("--min-supported", default=None)
    ap.add_argument("--expires", default=None)
    ap.add_argument("--api", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--no-activate", action="store_true")
    args = ap.parse_args(argv)

    import httpx
    with httpx.Client(timeout=600) as client:
        out = publish(
            client, base_url=args.api, token=args.token, file_path=args.file,
            platform=args.platform, version=args.version, channel=args.channel,
            rollout=args.rollout, mandatory=args.mandatory or None,
            min_supported=args.min_supported, expires=args.expires,
            activate=not args.no_activate,
        )
    print(f"Published {out['platform']} {out['version']} ({out['installer_type']}) "
          f"id={out['id']} rollout={args.rollout}% mandatory={args.mandatory} channel={args.channel}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
