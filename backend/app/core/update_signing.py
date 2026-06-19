"""Ed25519 signing for agent update manifests. The agent embeds the matching
public key and trusts ONLY it. NEVER raises into request handlers — verify()
returns False on any error."""
from __future__ import annotations

import base64
import hashlib
import logging
from functools import lru_cache

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)

from app.core.config import get_settings

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _private_key() -> Ed25519PrivateKey:
    raw = get_settings().AGENT_SIGNING_PRIVATE_KEY.strip()
    if raw:
        seed = base64.b64decode(raw)
        return Ed25519PrivateKey.from_private_bytes(seed)
    # Dev/test fallback: stable key derived from JWT_SECRET (NOT for prod).
    log.warning("AGENT_SIGNING_PRIVATE_KEY unset — deriving a dev signing key "
                "from JWT_SECRET. Set a real key before production.")
    seed = hashlib.sha256(("agent-update-signing:" + get_settings().JWT_SECRET).encode()).digest()
    return Ed25519PrivateKey.from_private_bytes(seed)


def sign(data: str) -> str:
    sig = _private_key().sign(data.encode("utf-8"))
    return base64.b64encode(sig).decode("ascii")


def verify(data: str, sig_b64: str) -> bool:
    try:
        pub = _private_key().public_key()
        pub.verify(base64.b64decode(sig_b64), data.encode("utf-8"))
        return True
    except Exception:
        return False


def public_key_b64() -> str:
    from cryptography.hazmat.primitives import serialization
    raw = _private_key().public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")
