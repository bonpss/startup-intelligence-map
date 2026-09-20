# python seed_owner.py
"""One-off owner-account bootstrap (spec-public-demo-auth.md).

The owner account is deliberately NOT created through POST /api/signup --
that endpoint rejects any email matching OWNER_EMAIL (403 "reserved"), so a
stranger can never claim the owner slot by signing up first (step-04 review,
iteration 1: the original design flagged owner status at signup time, which
let anyone who knew/guessed OWNER_EMAIL permanently lock the real owner out
of /graph). Run this once per deploy, before sharing the public link, to
create that one exempt-from-the-cap account directly.
"""

import getpass
import os
import sys

from dotenv import load_dotenv

import auth
import storage

load_dotenv()


def main() -> None:
    email = auth.normalize_email(os.environ.get("OWNER_EMAIL"))
    if not email:
        print("OWNER_EMAIL is not set. Set it in .env before running this script.", file=sys.stderr)
        sys.exit(1)

    if storage.get_user_by_email(email) is not None:
        print(f"A user already exists for {email} -- refusing to seed a duplicate.", file=sys.stderr)
        sys.exit(1)

    password = getpass.getpass(f"Password for owner account ({email}): ")
    if len(password) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        sys.exit(1)
    if password != getpass.getpass("Confirm password: "):
        print("Passwords did not match.", file=sys.stderr)
        sys.exit(1)

    password_hash = auth.hash_password(password)
    user = storage.create_user(email, password_hash, is_owner=True)
    if not user or not user.get("id"):
        print("Insert returned no row -- owner account was not created.", file=sys.stderr)
        sys.exit(1)

    print(f"Owner account created for {email}.")


if __name__ == "__main__":
    main()
