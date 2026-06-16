"""
Generate a new Fernet key for SECRETS_KEY.

Usage:
    docker compose exec backend python -m app.scripts.generateSecretsKey
"""
from cryptography.fernet import Fernet

key = Fernet.generate_key().decode()
print("\nAdd this to your .env file:\n")
print(f"SECRETS_KEY={key}\n")
print("Keep this key safe — losing it means you cannot decrypt your secrets files.")
