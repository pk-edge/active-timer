"""
Encrypt a Google credentials JSON file for safe storage in the repo.

The encrypted output (secrets/google_credentials.enc) is safe to commit.
The original .json file must NOT be committed — it stays gitignored.

Usage:
    # First generate a key if you haven't:
    docker compose exec backend python -m app.scripts.generateSecretsKey

    # Then encrypt (pass the JSON file path inside the container):
    docker compose exec backend python -m app.scripts.encryptSecrets \
        /path/to/google_credentials.json

    # Or from the host, piping the file in:
    docker compose exec -T backend python -m app.scripts.encryptSecrets - \
        < /path/to/google_credentials.json
"""
import json
import os
import sys
from pathlib import Path


def main() -> None:
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        print("ERROR: cryptography not installed. Run: pip install cryptography")
        sys.exit(1)

    raw_key = os.getenv("SECRETS_KEY", "").strip()
    if not raw_key:
        print("ERROR: SECRETS_KEY is not set. Run generateSecretsKey first.")
        sys.exit(1)

    try:
        f = Fernet(raw_key.encode())
    except Exception:
        print("ERROR: SECRETS_KEY is invalid.")
        sys.exit(1)

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    src = sys.argv[1]
    if src == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(src).read_text()

    # Validate it's real JSON before encrypting.
    try:
        json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: Input is not valid JSON: {e}")
        sys.exit(1)

    encrypted = f.encrypt(raw.encode())

    out_dir = Path(__file__).resolve().parent.parent.parent / "secrets"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "google_credentials.enc"
    out_path.write_bytes(encrypted)

    print(f"Encrypted → {out_path}")
    print("This file is safe to commit. Do NOT commit the original .json file.")


if __name__ == "__main__":
    main()
