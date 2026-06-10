"""
Bootstrap a company + first admin user.

Usage (run from backend/ directory):
    python -m app.scripts.seedAdmin \
        --company "Acme Inc" \
        --email   admin@acme.com \
        --name    "Admin" \
        --password "ChangeMe123!"

Prints the company enrollment_token which is embedded into agent installers.
"""

import argparse
import asyncio
import getpass
import sys

from app.db.admin import ensure_admin
from app.db.base import AsyncSessionLocal, init_db


async def run(company_name: str, email: str, name: str, password: str) -> None:
    await init_db()
    async with AsyncSessionLocal() as db:
        company, created = await ensure_admin(
            db, company_name=company_name, email=email, name=name, password=password
        )
        await db.commit()

    print(f"Admin {'created' if created else 'updated'}: {email.lower()}  (company: {company.name})")
    print("\n" + "─" * 60)
    print(f"Enrollment token:  {company.enrollment_token}")
    print("Embed this in the agent installer.")
    print("─" * 60)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--company",  required=True)
    p.add_argument("--email",    required=True)
    p.add_argument("--name",     required=True)
    p.add_argument("--password", default=None)
    args = p.parse_args()

    pwd = args.password or getpass.getpass("Admin password: ")
    if len(pwd) < 8:
        sys.exit("Password must be at least 8 characters.")

    asyncio.run(run(args.company, args.email, args.name, pwd))


if __name__ == "__main__":
    main()
