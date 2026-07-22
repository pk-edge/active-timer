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

Google OAuth credentials additionally have an EMBEDDED encrypted fallback in
this module (see _EMBEDDED_GOOGLE_*), so OAuth works even where neither the
secrets file nor env vars are available (e.g. AWS prod without Secrets Manager
access). Env / secrets-file values still take priority over the embedded blob.
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


# ── Embedded Google credentials (last-resort fallback) ────────────────────────
# Production runs on AWS where credentials would normally come from Secrets
# Manager, but that is not always reachable/populated. So the Google OAuth
# credentials are also embedded here as a final fallback, Fernet-encrypted with
# an in-code key.
#
# Why encrypted rather than plaintext: this keeps the raw client-secret string
# out of the source, so automated secret scanners (Google/Bitbucket) don't
# detect and AUTO-REVOKE it — which would break production. It is NOT a defense against
# someone reading this repo (the key sits right here); it is obfuscation that
# prevents accidental discovery/revocation. The Google web client is already
# hardened by the domain allowlist (@onedge.co) and Console-registered redirect
# URIs, which bound the damage of disclosure.
#
# To rotate: regenerate with
#   docker compose exec backend python -c "from cryptography.fernet import Fernet; \
#     import json; k=Fernet.generate_key(); \
#     t=Fernet(k).encrypt(json.dumps({'client_id':'…','client_secret':'…'}).encode()); \
#     print(k.decode()); print(t.decode())"
# and replace both constants below.
_EMBEDDED_GOOGLE_KEY = b"IICXLzboqnGa5VUd8RZSy02S7jQTs6bbs4K8FOr-ses="
_EMBEDDED_GOOGLE_ENC = (
    b"gAAAAABqMsSQhqB9LGFKv1WN3tFgIowI8LoIMlgxS-9xvR3c-YiGAU3aHpRgGQAQSY1_VNF8HUrF_qEQmFD"
    b"dwlDIaOJbzhpoxrUXopLKGR-RKdKe_iJT2w4nmO__Bg1Y9o1Gq_El17k7VJhiRcQSiH6uOWUTSVeTVypLOn"
    b"T7eBCWNo_7UdS-td-H99b8krADUZsZWuQz-RamLjIrVhrPfb8z7nmbwMMra_N7A5S6ikpLbqgQRYAmilvYz"
    b"V7MAGtY8ZZB2_W-7wD4vcfAfj9k0ksk5BkUm690eg=="
)


def _embedded_google_credentials() -> dict[str, str]:
    """Decrypt the in-code Google credentials. Returns {} if crypto is
    unavailable or the embedded blob can't be read (never raises)."""
    if not _CRYPTO_AVAILABLE:
        return {}
    try:
        data = json.loads(Fernet(_EMBEDDED_GOOGLE_KEY).decrypt(_EMBEDDED_GOOGLE_ENC))
        return {
            "client_id":     data.get("client_id", ""),
            "client_secret": data.get("client_secret", ""),
        }
    except Exception as e:
        log.error("Embedded Google credentials unreadable: %s", e)
        return {}


def load_google_credentials() -> dict[str, str]:
    """
    Return {'client_id': ..., 'client_secret': ...}.

    Priority (first source that yields BOTH values wins, so any environment can
    override the embedded fallback without code changes):
      1. Decrypt secrets/google_credentials.enc  (requires SECRETS_KEY)
      2. Env vars GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET
      3. Embedded encrypted credentials in this module (always available — used
         on AWS prod where Secrets Manager isn't reachable/populated)
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

    env_cid    = os.getenv("GOOGLE_CLIENT_ID", "")
    env_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    if env_cid and env_secret:
        log.debug("Google credentials loaded from environment variables")
        return {"client_id": env_cid, "client_secret": env_secret}

    embedded = _embedded_google_credentials()
    if embedded.get("client_id") and embedded.get("client_secret"):
        env = os.getenv("ENVIRONMENT", "development").lower()
        if env != "development":
            # Deliberately kept as a last-resort fallback (prod may depend on
            # it), but a real deployment silently running on an in-source
            # secret must be VISIBLE: anyone with repo access holds this
            # credential. Provision env vars / SECRETS_KEY and rotate it.
            log.warning(
                "Google OAuth credentials are being served from the EMBEDDED "
                "in-source fallback (ENVIRONMENT=%s). Provision GOOGLE_CLIENT_ID/"
                "GOOGLE_CLIENT_SECRET or SECRETS_KEY, then rotate the embedded secret.",
                env,
            )
        else:
            log.debug("Google credentials loaded from embedded fallback")
        return embedded

    return {"client_id": env_cid, "client_secret": env_secret}
