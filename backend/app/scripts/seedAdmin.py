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
import secrets
import sys

from sqlalchemy import select

from app.db.base import AsyncSessionLocal, init_db
from app.db.models import Company, Employee
from app.core.security import hash_password


async def run(company_name: str, email: str, name: str, password: str) -> None:
    await init_db()
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Company).where(Company.name == company_name))
        company = result.scalar_one_or_none()
        if not company:
            company = Company(name=company_name, enrollment_token=secrets.token_hex(24))
            db.add(company)
            await db.flush()
            print(f"Created company: {company.name}")
        else:
            print(f"Using existing company: {company.name}")

        email = email.lower()
        result = await db.execute(select(Employee).where(Employee.email == email))
        emp = result.scalar_one_or_none()
        if emp:
            emp.password_hash = hash_password(password)
            emp.role = "admin"
            emp.is_active = True
            emp.name = name
            print(f"Updated: {email}")
        else:
            emp = Employee(
                company_id=company.id, email=email, name=name,
                role="admin", password_hash=hash_password(password),
            )
            db.add(emp)
            print(f"Created admin: {email}")

        await db.commit()

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
