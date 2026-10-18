import argparse
import getpass

from pwdlib import PasswordHash
from sqlalchemy import select

from .db import SessionLocal
from .models import User
from .storage import get_storage


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("create-user")
    c.add_argument("email")
    c.add_argument("name")
    c.add_argument("--admin", action="store_true")
    sub.add_parser("init-storage")
    a = p.parse_args()
    if a.command == "init-storage":
        get_storage().create_bucket()
        print("Storage bucket ready")
        return
    password = getpass.getpass("Password (min 12 chars): ")
    if len(password) < 12:
        raise SystemExit("Password must be at least 12 characters")
    with SessionLocal() as s:
        if s.scalar(select(User).where(User.email == a.email.lower())):
            raise SystemExit("User already exists")
        s.add(
            User(
                email=a.email.lower(),
                name=a.name,
                password=PasswordHash.recommended().hash(password),
                is_admin=a.admin,
            )
        )
        s.commit()
    print("User created")


if __name__ == "__main__":
    main()
