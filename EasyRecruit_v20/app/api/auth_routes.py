"""
EasyRecruit ATS 3.0 — Authentication Routes
Improvements over v2.0:
  • Token blacklisting for real logout
  • Proper request-body for change-password (no query-params)
  • login_count / last_login_at tracking
  • UUID jti in JWT for revocation
  • Password-strength validation reused from schema
"""
import uuid
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
import bcrypt as _bcrypt_lib
from fastapi import APIRouter, Depends, Header, HTTPException, status

# ── Password helpers using bcrypt directly (no passlib dependency) ──────────
# passlib 1.7.4 is incompatible with bcrypt 4.1+ due to removed __about__
# attribute. We call bcrypt directly — same algorithm, zero passlib issues.

def _encode(s: str) -> bytes:
    return s.encode("utf-8") if isinstance(s, str) else s

def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _bcrypt_lib.checkpw(_encode(plain), _encode(hashed))
    except Exception:
        return False

def hash_password(plain: str) -> str:
    return _bcrypt_lib.hashpw(_encode(plain), _bcrypt_lib.gensalt(rounds=12)).decode("utf-8")
# ─────────────────────────────────────────────────────────────────────────────

from app.config import settings
from app.db.database import db
from app.models.schemas import (
    ChangePasswordRequest, TokenResponse, UserCreate,
    UserLogin, UserResponse, UserRole, UserUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])




# ────────────────────────────────────────────────────────────────────────────
# JWT utilities
# ────────────────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_access_token(user_id: int) -> tuple[str, str, datetime]:
    """Return (encoded_token, jti, expiry)."""
    jti    = str(uuid.uuid4())
    expiry = _utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user_id),
        "jti": jti,
        "iat": _utcnow(),
        "exp": expiry,
        "type": "access",
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return token, jti, expiry


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def _is_blacklisted(jti: str) -> bool:
    row = db.fetch_one(
        "SELECT id FROM token_blacklist WHERE jti = ?", (jti,)
    )
    return row is not None


def _blacklist_token(jti: str, user_id: int, expires_at: datetime):
    db.insert(
        "INSERT OR IGNORE INTO token_blacklist (jti, user_id, expires_at) VALUES (?, ?, ?)",
        (jti, user_id, expires_at.isoformat()),
    )


# ────────────────────────────────────────────────────────────────────────────
# User lookup helpers
# ────────────────────────────────────────────────────────────────────────────

def get_user_by_email(email: str) -> Optional[dict]:
    return db.fetch_one(
        "SELECT * FROM users WHERE email = ? AND is_active = 1", (email,)
    )


def get_user_by_id(user_id: int) -> Optional[dict]:
    return db.fetch_one(
        "SELECT * FROM users WHERE id = ? AND is_active = 1", (user_id,)
    )


def get_company_by_name(name: str) -> Optional[dict]:
    return db.fetch_one(
        "SELECT * FROM companies WHERE LOWER(name) = LOWER(?)", ((name or "").strip(),)
    )


# ────────────────────────────────────────────────────────────────────────────
# FastAPI dependencies
# ────────────────────────────────────────────────────────────────────────────

async def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    creds_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization.removeprefix("Bearer ").strip()
    payload = decode_token(token)
    if not payload:
        raise creds_exc

    jti = payload.get("jti")
    if jti and _is_blacklisted(jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked. Please log in again.",
        )

    user_id = payload.get("sub")
    if not user_id:
        raise creds_exc

    user = get_user_by_id(int(user_id))
    if not user:
        raise creds_exc

    return user


async def get_optional_user(authorization: Optional[str] = Header(None)) -> Optional[dict]:
    if not authorization:
        return None
    try:
        return await get_current_user(authorization)
    except HTTPException:
        return None


# ────────────────────────────────────────────────────────────────────────────
# Routes
# ────────────────────────────────────────────────────────────────────────────

@router.get("/companies", response_model=list)
async def list_companies():
    """
    Public: every company registered on EasyRecruit, with the e-mail domain
    a recruiter must use to sign up for it. Powers the "Company" dropdown
    on the registration form.
    """
    return db.fetch_all("SELECT name, recruiter_domain FROM companies ORDER BY name")


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(user_data: UserCreate):
    """Register a new user account.

    - Candidates are created and approved instantly, as before.
    - Recruiters must select a company that's registered on the platform AND
      use that company's official recruiter e-mail domain — otherwise the
      request is rejected outright. If both checks pass, the account is
      still created only in 'pending' status: it cannot sign in until that
      company's admin approves it.
    - admin / company_admin accounts can't be created through this endpoint
      at all — they're provisioned directly.
    """
    if user_data.role in (UserRole.ADMIN, UserRole.COMPANY_ADMIN):
        raise HTTPException(
            status_code=403,
            detail="This account type can't be self-registered. Contact your platform administrator.",
        )

    if get_user_by_email(user_data.email):
        raise HTTPException(status_code=400, detail="Email already registered.")

    existing_username = db.fetch_one(
        "SELECT id FROM users WHERE username = ?", (user_data.username,)
    )
    if existing_username:
        raise HTTPException(status_code=400, detail="Username already taken.")

    approval_status = "approved"

    if user_data.role == UserRole.RECRUITER:
        company = get_company_by_name(user_data.company_name or "")
        if not company:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"'{user_data.company_name}' is not a company registered on EasyRecruit. "
                    "Please choose a company from the list."
                ),
            )
        email_domain = user_data.email.rsplit("@", 1)[-1].lower()
        required_domain = company["recruiter_domain"].lower()
        if email_domain != required_domain:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Recruiters for {company['name']} must register with an official "
                    f"'@{required_domain}' e-mail address, not a personal e-mail like Gmail."
                ),
            )
        # Normalise to the canonical stored casing of the company name.
        user_data.company_name = company["name"]
        approval_status = "pending"

    password_hash = hash_password(user_data.password)

    try:
        user_id = db.insert(
            """INSERT INTO users (email, username, password_hash, full_name, company_name, role, approval_status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                user_data.email,
                user_data.username,
                password_hash,
                user_data.full_name,
                user_data.company_name,
                user_data.role.value,
                approval_status,
            ),
        )
        logger.info(f"User registered: {user_data.email} (id={user_id}, approval={approval_status})")
        return get_user_by_id(user_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Registration error for {user_data.email}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Account creation failed. Please try again.")


@router.post("/login", response_model=TokenResponse)
async def login(credentials: UserLogin):
    """Authenticate and return JWT access token."""
    user = get_user_by_email(credentials.email)
    if not user or not verify_password(credentials.password, user["password_hash"]):
        logger.warning(f"Failed login attempt for email: {credentials.email}")
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    approval_status = user.get("approval_status") or "approved"
    company_label = user.get("company_name") or "your company"
    if approval_status == "pending":
        raise HTTPException(
            status_code=403,
            detail=(
                f"Your recruiter account for {company_label} is awaiting approval from "
                f"{company_label}'s admin. You'll be able to sign in once it's approved."
            ),
        )
    if approval_status == "rejected":
        raise HTTPException(
            status_code=403,
            detail=(
                f"Your recruiter access request for {company_label} was declined by "
                f"their admin. Please contact {company_label} directly for details."
            ),
        )

    token, _jti, expiry = create_access_token(user["id"])

    # Update last-login stats (non-critical — don't fail login if this errors)
    try:
        db.execute(
            """UPDATE users
               SET last_login_at = CURRENT_TIMESTAMP,
                   login_count   = login_count + 1,
                   updated_at    = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (user["id"],),
        )
    except Exception as exc:
        logger.warning(f"Could not update login stats for user {user['id']}: {exc}")

    logger.info(f"Login: {user['email']}")

    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "user": get_user_by_id(user["id"]),
    }


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: dict = Depends(get_current_user)):
    """Return the currently authenticated user."""
    return current_user


@router.put("/me", response_model=UserResponse)
async def update_me(
    user_update: UserUpdate,
    current_user: dict = Depends(get_current_user),
):
    """Update current user's profile fields."""
    fields, values = [], []

    mapping = {
        "email":        user_update.email,
        "username":     user_update.username,
        "full_name":    user_update.full_name,
    }
    for col, val in mapping.items():
        if val is not None:
            fields.append(f"{col} = ?")
            values.append(str(val))

    # role / is_active / company_name are deliberately NOT updatable here —
    # see the admin-only /admin/users/{id}/role and /toggle endpoints.

    if not fields:
        return current_user

    fields.append("updated_at = CURRENT_TIMESTAMP")
    values.append(current_user["id"])

    try:
        db.execute(
            f"UPDATE users SET {', '.join(fields)} WHERE id = ?",
            tuple(values),
        )
        return get_user_by_id(current_user["id"])
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Profile update error for user {current_user['id']}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Profile update failed. Please try again.")


@router.post("/change-password", response_model=dict)
async def change_password(
    body: ChangePasswordRequest,
    current_user: dict = Depends(get_current_user),
):
    """Change the authenticated user's password."""
    if not verify_password(body.current_password, current_user["password_hash"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")

    new_hash = hash_password(body.new_password)
    db.execute(
        "UPDATE users SET password_hash = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (new_hash, current_user["id"]),
    )
    logger.info(f"Password changed: {current_user['email']}")
    return {"success": True, "message": "Password changed successfully."}


@router.post("/logout", response_model=dict)
async def logout(
    authorization: Optional[str] = Header(None),
    current_user: dict = Depends(get_current_user),
):
    """Revoke the current token (add to blacklist)."""
    if authorization:
        token = authorization.removeprefix("Bearer ").strip()
        payload = decode_token(token)
        if payload:
            jti = payload.get("jti")
            exp = payload.get("exp")
            if jti and exp:
                expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
                _blacklist_token(jti, current_user["id"], expires_at)

    logger.info(f"Logout: {current_user['email']}")
    return {"success": True, "message": "Logged out successfully."}


# ────────────────────────────────────────────────────────────────────────────
# Admin-only routes
# ────────────────────────────────────────────────────────────────────────────

async def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """Dependency: only admin users may proceed."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    return current_user


@router.get("/admin/users", response_model=list)
async def admin_list_users(admin: dict = Depends(require_admin)):
    """[Admin] List all registered users."""
    rows = db.fetch_all(
        "SELECT id, email, username, full_name, company_name, role, is_active, "
        "login_count, last_login_at, created_at FROM users ORDER BY created_at DESC"
    )
    return rows


@router.patch("/admin/users/{user_id}/role", response_model=dict)
async def admin_change_role(user_id: int, body: dict, admin: dict = Depends(require_admin)):
    """[Admin] Change a user's role."""
    new_role = body.get("role")
    valid_roles = {"recruiter", "admin", "candidate", "company_admin"}
    if new_role not in valid_roles:
        raise HTTPException(status_code=400, detail=f"Invalid role. Choose from: {valid_roles}")
    affected = db.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
    if not affected:
        raise HTTPException(status_code=404, detail="User not found.")
    return {"success": True, "user_id": user_id, "new_role": new_role}


@router.patch("/admin/users/{user_id}/toggle", response_model=dict)
async def admin_toggle_user(user_id: int, admin: dict = Depends(require_admin)):
    """[Admin] Activate or deactivate a user."""
    user = db.fetch_one("SELECT id, is_active FROM users WHERE id = ?", (user_id,))
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    new_state = 0 if user["is_active"] else 1
    db.execute("UPDATE users SET is_active = ? WHERE id = ?", (new_state, user_id))
    return {"success": True, "user_id": user_id, "is_active": bool(new_state)}


@router.delete("/admin/users/{user_id}", response_model=dict)
async def admin_delete_user(user_id: int, admin: dict = Depends(require_admin)):
    """[Admin] Permanently delete a user."""
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own account.")
    affected = db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    if not affected:
        raise HTTPException(status_code=404, detail="User not found.")
    return {"success": True, "deleted_user_id": user_id}


@router.get("/admin/stats", response_model=dict)
async def admin_stats(admin: dict = Depends(require_admin)):
    """[Admin] System-wide statistics."""
    total_users     = db.fetch_one("SELECT COUNT(*) as c FROM users")["c"]
    active_users    = db.fetch_one("SELECT COUNT(*) as c FROM users WHERE is_active=1")["c"]
    total_analyses  = db.fetch_one("SELECT COUNT(*) as c FROM resume_analyses")["c"]
    avg_score       = db.fetch_one("SELECT AVG(overall_score) as a FROM resume_analyses")["a"] or 0
    total_jobs      = db.fetch_one("SELECT COUNT(*) as c FROM job_descriptions")["c"]
    by_role         = db.fetch_all("SELECT role, COUNT(*) as cnt FROM users GROUP BY role")
    return {
        "total_users":    total_users,
        "active_users":   active_users,
        "total_analyses": total_analyses,
        "avg_score":      round(avg_score, 1),
        "total_jobs":     total_jobs,
        "users_by_role":  {r["role"]: r["cnt"] for r in by_role},
    }


# ────────────────────────────────────────────────────────────────────────────
# Company-admin routes — approve/reject recruiters who registered for THEIR
# company. A platform-wide 'admin' may also act here, across any company,
# as an oversight safety valve; a 'company_admin' is restricted to their own.
# ────────────────────────────────────────────────────────────────────────────

async def require_company_admin(current_user: dict = Depends(get_current_user)) -> dict:
    if current_user.get("role") not in ("company_admin", "admin"):
        raise HTTPException(status_code=403, detail="Company admin access required.")
    return current_user


def _scope_to_own_company(admin: dict, target: dict) -> None:
    """Raise 403 unless `admin` is a site admin or owns the same company as `target`."""
    if admin.get("role") == "admin":
        return
    if (target.get("company_name") or "").strip().lower() != (admin.get("company_name") or "").strip().lower():
        raise HTTPException(status_code=403, detail="You can only manage recruiters registered under your own company.")


@router.get("/company-admin/team", response_model=dict)
async def company_admin_team(admin: dict = Depends(require_company_admin)):
    """List every recruiter for the admin's company (any approval status)."""
    if admin.get("role") == "admin":
        rows = db.fetch_all(
            "SELECT id, email, username, full_name, company_name, approval_status, "
            "is_active, created_at FROM users WHERE role='recruiter' ORDER BY created_at DESC"
        )
        company_label = "All companies"
    else:
        company_label = admin.get("company_name") or ""
        rows = db.fetch_all(
            "SELECT id, email, username, full_name, company_name, approval_status, "
            "is_active, created_at FROM users WHERE role='recruiter' AND LOWER(company_name)=LOWER(?) "
            "ORDER BY created_at DESC",
            (company_label,),
        )
    return {"company": company_label, "total": len(rows), "recruiters": rows}


@router.post("/company-admin/approve/{user_id}", response_model=dict)
async def company_admin_approve(user_id: int, admin: dict = Depends(require_company_admin)):
    target = db.fetch_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    if target.get("role") != "recruiter":
        raise HTTPException(status_code=400, detail="Only recruiter sign-ups can be approved here.")
    _scope_to_own_company(admin, target)
    db.execute(
        "UPDATE users SET approval_status='approved', approved_by=?, approved_at=CURRENT_TIMESTAMP, "
        "updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (admin["id"], user_id),
    )
    logger.info(f"Recruiter {target['email']} approved by {admin['email']}")
    return {"success": True, "user_id": user_id, "approval_status": "approved"}


@router.post("/company-admin/reject/{user_id}", response_model=dict)
async def company_admin_reject(user_id: int, admin: dict = Depends(require_company_admin)):
    target = db.fetch_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    if target.get("role") != "recruiter":
        raise HTTPException(status_code=400, detail="Only recruiter sign-ups can be rejected here.")
    _scope_to_own_company(admin, target)
    db.execute(
        "UPDATE users SET approval_status='rejected', approved_by=?, approved_at=CURRENT_TIMESTAMP, "
        "updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (admin["id"], user_id),
    )
    logger.info(f"Recruiter {target['email']} rejected by {admin['email']}")
    return {"success": True, "user_id": user_id, "approval_status": "rejected"}
