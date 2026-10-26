"""
Operator CLI for privileged accounts.

    python -m app.admin_cli list-admins
    python -m app.admin_cli provision --role company_admin --company "Zoho Corporation" --email ops@example.org
    python -m app.admin_cli provision --role admin --email owner@example.org --generate
    python -m app.admin_cli rotate --email admin@example.org [--generate] [--yes]

Password sources (never a command-line argument, so it can't leak via shell
history or the process list), in order:
    --generate                      random 20-char password, printed ONCE to this terminal
    EASYRECRUIT_ADMIN_PASSWORD      environment variable (non-interactive use)
    interactive prompt              entered twice, not echoed

Target database: --db PATH, else DATABASE_PATH (also read from .env), else the
default app/data/easyrecruit.db. The target path is always printed first.
Back up a production database before running rotate/provision against it.
"""
import argparse
import getpass
import os
import sys
from typing import List, Optional, Tuple

from app.db.admin_provisioning import (
    PRIVILEGED_ROLES, ProvisioningError, generate_password,
    list_privileged_accounts, provision_account, rotate_password,
)


def _open_db(path_arg: Optional[str], must_exist: bool):
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    from app.db.database import DatabaseManager, DEFAULT_DB_PATH
    path = path_arg or os.getenv("DATABASE_PATH") or DEFAULT_DB_PATH
    print(f"Target database: {os.path.abspath(path)}")
    if must_exist and not os.path.exists(path):
        raise ProvisioningError("database file does not exist; refusing to create an empty one")
    return DatabaseManager(path)


def _password(args) -> Tuple[str, bool]:
    if args.generate:
        return generate_password(), True
    env_pw = os.environ.get("EASYRECRUIT_ADMIN_PASSWORD")
    if env_pw:
        return env_pw, False
    if not sys.stdin.isatty():
        raise ProvisioningError("no password source: use a terminal, --generate, "
                                "or EASYRECRUIT_ADMIN_PASSWORD")
    first = getpass.getpass("New password: ")
    if first != getpass.getpass("Confirm password: "):
        raise ProvisioningError("passwords did not match")
    return first, False


def _show_generated(pw: str, generated: bool) -> None:
    if generated:
        print(f"Generated password (shown once; store it securely): {pw}")


def _cmd_list(args) -> int:
    db = _open_db(args.db, must_exist=True)
    rows = list_privileged_accounts(db)
    print(f"{'id':>4}  {'role':<13} {'active':<6} {'approval':<9} {'logins':>6}  "
          f"{'last_login':<19}  email / company")
    for r in rows:
        print(f"{r['id']:>4}  {r['role']:<13} {('yes' if r['is_active'] else 'NO'):<6} "
              f"{(r['approval_status'] or ''):<9} {(r['login_count'] or 0):>6}  "
              f"{str(r['last_login_at'] or '-')[:19]:<19}  {r['email']}  [{r['company_name'] or '-'}]")
    print(f"{len(rows)} privileged account(s).")
    return 0


def _cmd_provision(args) -> int:
    db = _open_db(args.db, must_exist=False)
    db.init_database()                      # idempotent; safe on fresh or existing DBs
    from app.db.company_seed import seed_companies
    seed_companies(db)                      # registry only; adds missing companies
    pw, generated = _password(args)
    res = provision_account(db, email=args.email, password=pw, role=args.role,
                            company_name=args.company, username=args.username,
                            full_name=args.full_name)
    print(f"Created {res['role']} account {res['email']} (username {res['username']}).")
    _show_generated(pw, generated)
    return 0


def _cmd_rotate(args) -> int:
    db = _open_db(args.db, must_exist=True)
    if not args.yes:
        if not sys.stdin.isatty():
            raise ProvisioningError("non-interactive rotate requires --yes")
        if input(f"Rotate password for {args.email}? [y/N] ").strip().lower() != "y":
            print("Aborted; nothing changed.")
            return 1
    pw, generated = _password(args)
    res = rotate_password(db, email=args.email, new_password=pw)
    print(f"Rotated password for {res['email']} ({res['role']}). Existing login sessions stay "
          "valid until they expire; rotate SECRET_KEY to end them immediately.")
    _show_generated(pw, generated)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.admin_cli", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", help="path to the SQLite database (default: DATABASE_PATH or app/data)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list-admins", help="list admin/company_admin accounts (no hashes)")

    pr = sub.add_parser("provision", help="create an admin or company_admin account")
    pr.add_argument("--email", required=True)
    pr.add_argument("--role", choices=PRIVILEGED_ROLES, default="admin")
    pr.add_argument("--company", help="registered company name (required for company_admin)")
    pr.add_argument("--username")
    pr.add_argument("--full-name", dest="full_name")
    pr.add_argument("--generate", action="store_true", help="generate a random password")

    ro = sub.add_parser("rotate", help="set a new password on an existing admin account")
    ro.add_argument("--email", required=True)
    ro.add_argument("--generate", action="store_true", help="generate a random password")
    ro.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    handler = {"list-admins": _cmd_list, "provision": _cmd_provision, "rotate": _cmd_rotate}
    try:
        return handler[args.command](args)
    except ProvisioningError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
