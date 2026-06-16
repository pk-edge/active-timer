"""
Encrypted secrets loader using Fernet symmetric encryption.

Secrets are stored as Fernet-encrypted JSON files in backend/secrets/.
The decryption key (SECRETS_KEY) lives only in .env — never in the repo.

Setup
─────
1. Generate a key (run once per environment):
       docker compose exec backend python -m app.scripts.generateSecretsKey

2. Add the printed key to .env:
       SECRETS_KEY=<base64-fernet-key>

3. Encrypt your Google credentials JSON:
       docker compose exec backend python -m app.scripts.encryptSecrets \
           /path/to/google_credentials.json

4. Commit secrets/google_credentials.enc — the .json source stays gitignored.
"""
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_SECRETS_DIR = Path(__file__).resolve().parent.parent.parent / "secrets"

try:
    from cryptography.fernet import Fernet, InvalidToken
    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False
    log.warning("cryptography package not installed — encrypted secrets unavailable")


def _fernet():
    if not _CRYPTO_AVAILABLE:
        return None
    raw = os.getenv("SECRETS_KEY", "").strip()
    if not raw:
        return None
    try:
        return Fernet(raw.encode())
    except Exception:
        log.error("SECRETS_KEY is set but invalid — ignoring encrypted secrets file")
        return None


def _decrypt_json(filename: str) -> dict:
    f = _fernet()
    if not f:
        return {}
    path = _SECRETS_DIR / filename
    if not path.exists():
        return {}
    try:
        return json.loads(f.decrypt(path.read_bytes()))
    except InvalidToken:
        log.error("Could not decrypt %s — wrong SECRETS_KEY?", filename)
        return {}
    except Exception as e:
        log.error("Failed to load %s: %s", filename, e)
        return {}


def load_google_credentials() -> dict[str, str]:
    """
    Return {'client_id': ..., 'client_secret': ...}.

    Priority:
      1. Decrypt secrets/google_credentials.enc  (requires SECRETS_KEY)
      2. Env vars GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET
    """
    data = _decrypt_json("google_credentials.enc")
    if data:
        # Google JSON wraps credentials under a "web" key.
        inner = data.get("web", data)
        cid    = inner.get("client_id", "")
        csecret = inner.get("client_secret", "")
        if cid and csecret:
            log.debug("Google credentials loaded from encrypted secrets file")
            return {"client_id": cid, "client_secret": csecret}

    return {
        "client_id":     os.getenv("GOOGLE_CLIENT_ID", ""),
        "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
    }
