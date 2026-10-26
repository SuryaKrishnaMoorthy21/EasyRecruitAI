"""
EasyRecruit ATS — Privileged account provisioning
─────────────────────────────────────────────────
The ONLY supported way to create or re-key ``admin`` / ``company_admin``
accounts. No credential is stored in source control; the secret is chosen by
the operator at provisioning time and is never logged or printed by this module.

Entry points
  provision_account(...)                  create one privileged account
  bootstrap_initial_admin_from_env(db)    opt-in first-admin on a FRESH deployment
  rotate_password(...)                    set a new password on an existing account
  list_privileged_accounts(db)            read-only audit listing (no hashes)

Operator CLI: ``python -m app.admin_cli --help``

Design rules
  * Existing accounts are never overwritten, disabled or deleted by provisioning.
    ``rotate_password`` changes only ``password_hash`` (+ ``updated_at``).
  * The env bootstrap acts only when NO privileged account exists at all, so
    leaving INITIAL_ADMIN_* set cannot create extra accounts later.
  * Error messages and logs never contain the password.

This module deliberately does not import the web layer (FastAPI / JWT config),
so it works on a host that has no SECRET_KEY configured. Hashing parameters
mirror app.api.auth_routes (bcrypt, 12 rounds); tests assert the two stay
compatible.
"""
import logging
import os
import re
import secrets
import sqlite3
import string
from typing import Dict, List, Mapping, Optional

import bcrypt

logger = logging.getLogger(__name__)

PRIVILEGED_ROLES = ("admin", "company_admin")
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_BYTES = 72          # bcrypt limit; newer bcrypt raises beyond this

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_BLOCKED_SUBSTRINGS = (
    "password", "passw0rd", "changeme", "change-me", "change_me", "easyrecruit",
    "admin123", "qwerty", "letmein", "welcome", "12345678", "iloveyou",
)


class ProvisioningError(ValueError):
    """Raised for any refusal. Messages never contain the password."""


# ── hashing (kept compatible with app.api.auth_routes) ────────────────────────
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ── password policy (stricter than self-registration: these accounts are privileged)
def password_problems(password: str, email: Optional[str] = None,
                      username: Optional[str] = None) -> List[str]:
    problems: List[str] = []
    if not isinstance(password, str) or not password:
        return ["password is empty"]
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        problems.append(f"must be at most {MAX_PASSWORD_BYTES} bytes")
    if not re.search(r"[a-z]", password):
        problems.append("must contain a lowercase letter")
    if not re.search(r"[A-Z]", password):
        problems.append("must contain an uppercase letter")
    if not re.search(r"\d", password):
        problems.append("must contain a digit")
    low = password.lower()
    if any(b in low for b in _BLOCKED_SUBSTRINGS):
        problems.append("contains a common or product-specific word")
    for label, ident in (("e-mail", (email or "").split("@")[0]), ("username", username or "")):
        if len(ident) >= 3 and ident.lower() in low:
            problems.append(f"must not contain the {label} name")
    return problems


def generate_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits + "-_"
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if not password_problems(candidate):
            return candidate


# ── helpers ───────────────────────────────────────────────────────────────────
def _derive_username(db, email: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "", email.split("@")[0])[:30] or "admin"
    if len(base) < 3:
        base = (base + "adm")[:30]
    candidate, n = base, 1
    while db.fetch_one("SELECT 1 FROM users WHERE username = ?", (candidate,)):
        n += 1
        candidate = f"{base[:26]}{n}"
    return candidate


def has_privileged_accounts(db) -> bool:
    row = db.fetch_one(
        "SELECT COUNT(*) AS n FROM users WHERE role IN ('admin', 'company_admin')"
    )
    return bool(row and row["n"])


# ── provisioning ──────────────────────────────────────────────────────────────
def provision_account(db, *, email: str, password: str, role: str = "admin",
                      company_name: Optional[str] = None,
                      username: Optional[str] = None,
                      full_name: Optional[str] = None) -> Dict:
    """Create one privileged account. Refuses (never overwrites) if it exists."""
    email = (email or "").strip()
    if role not in PRIVILEGED_ROLES:
        raise ProvisioningError(f"role must be one of: {', '.join(PRIVILEGED_ROLES)}")
    if not _EMAIL_RE.match(email):
        raise ProvisioningError("a valid e-mail address is required")
    if db.fetch_one("SELECT 1 FROM users WHERE LOWER(email) = LOWER(?)", (email,)):
        raise ProvisioningError(f"an account for {email} already exists; nothing was changed")

    canonical_company = None
    if role == "company_admin":
        if not company_name:
            raise ProvisioningError("company_admin requires a registered company name")
        row = db.fetch_one(
            "SELECT name FROM companies WHERE LOWER(name) = LOWER(?)", (company_name.strip(),)
        )
        if not row:
            raise ProvisioningError(f"company {company_name!r} is not in the company registry")
        canonical_company = row["name"]

    if username:
        username = username.strip()
        if not 3 <= len(username) <= 50:
            raise ProvisioningError("username must be 3-50 characters")
        if db.fetch_one("SELECT 1 FROM users WHERE username = ?", (username,)):
            raise ProvisioningError("that username is already taken")
    else:
        username = _derive_username(db, email)

    problems = password_problems(password, email=email, username=username)
    if problems:
        raise ProvisioningError("password rejected: " + "; ".join(problems))

    if not full_name:
        full_name = (f"{canonical_company} Company Admin" if role == "company_admin"
                     else "Platform Administrator")

    try:
        user_id = db.insert(
            """INSERT INTO users (email, username, password_hash, full_name, company_name,
                                  role, approval_status, is_active)
               VALUES (?, ?, ?, ?, ?, ?, 'approved', 1)""",
            (email, username, hash_password(password), full_name, canonical_company, role),
        )
    except sqlite3.IntegrityError:
        raise ProvisioningError("e-mail or username already in use; nothing was changed")

    logger.info(f"Provisioned {role} account id={user_id} email={email}")
    return {"id": user_id, "email": email, "username": username,
            "role": role, "company_name": canonical_company}


def bootstrap_initial_admin_from_env(db, environ: Optional[Mapping[str, str]] = None) -> str:
    """
    Opt-in first-administrator creation for fresh deployments where no shell is
    available (e.g. Render free tier). Variables:

        INITIAL_ADMIN_EMAIL, INITIAL_ADMIN_PASSWORD      (both required to act)
        INITIAL_ADMIN_ROLE      admin (default) | company_admin
        INITIAL_ADMIN_COMPANY   required when role is company_admin

    Acts ONLY if no admin/company_admin account exists. Remove the variables
    after the first successful start. Returns a short status string.
    """
    env = os.environ if environ is None else environ
    email = (env.get("INITIAL_ADMIN_EMAIL") or "").strip()
    password = env.get("INITIAL_ADMIN_PASSWORD") or ""

    if not email and not password:
        return "disabled"
    if not (email and password):
        logger.error("INITIAL_ADMIN_EMAIL and INITIAL_ADMIN_PASSWORD must both be set; "
                     "no account was created.")
        return "incomplete"
    if has_privileged_accounts(db):
        logger.warning("INITIAL_ADMIN_* variables are set but privileged accounts already "
                       "exist; they were ignored. Remove these variables from the environment.")
        return "skipped_existing"
    try:
        provision_account(
            db, email=email, password=password,
            role=(env.get("INITIAL_ADMIN_ROLE") or "admin").strip(),
            company_name=(env.get("INITIAL_ADMIN_COMPANY") or "").strip() or None,
        )
    except ProvisioningError as exc:
        logger.error(f"Initial admin NOT created: {exc}")
        return "invalid"
    logger.warning(f"Initial administrator {email} created. Remove INITIAL_ADMIN_PASSWORD "
                   "from the environment now.")
    return "created"


def rotate_password(db, *, email: str, new_password: str) -> Dict:
    """Set a new password on an existing admin/company_admin. Changes nothing else."""
    row = db.fetch_one(
        "SELECT id, email, username, role FROM users WHERE LOWER(email) = LOWER(?)",
        ((email or "").strip(),),
    )
    if not row:
        raise ProvisioningError("no account with that e-mail; nothing was changed")
    if row["role"] not in PRIVILEGED_ROLES:
        raise ProvisioningError("rotate is limited to admin / company_admin accounts")
    problems = password_problems(new_password, email=row["email"], username=row["username"])
    if problems:
        raise ProvisioningError("password rejected: " + "; ".join(problems))

    db.execute(
        "UPDATE users SET password_hash = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (hash_password(new_password), row["id"]),
    )
    stored = db.fetch_one("SELECT password_hash FROM users WHERE id = ?", (row["id"],))
    if not stored or not verify_password(new_password, stored["password_hash"]):
        raise ProvisioningError("post-update verification failed; investigate before relying on it")
    logger.info(f"Rotated password for {row['role']} id={row['id']} email={row['email']}")
    return {"id": row["id"], "email": row["email"], "role": row["role"]}


def list_privileged_accounts(db) -> List[Dict]:
    """Read-only audit view. Never returns password hashes."""
    rows = db.fetch_all(
        """SELECT id, email, role, company_name, is_active, approval_status,
                  last_login_at, login_count
           FROM users WHERE role IN ('admin', 'company_admin')
           ORDER BY role, company_name, email"""
    )
    return [dict(r) for r in rows]
