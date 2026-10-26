"""
EasyRecruit ATS — Account privilege regression tests.

Covers the role-escalation hole in PUT /api/v1/auth/me: the self-service
update must never let a user change their own role, active status, or
company (tenant) assignment. Those changes are admin-only and must go
through the protected /api/v1/auth/admin/... endpoints.

Every user created here gets a unique e-mail/username so the tests are
safe to re-run against a database that already contains earlier test data.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uuid

import pytest
from fastapi.testclient import TestClient

from main import app
from app.api.auth_routes import hash_password
from app.db.database import db
from app.models.schemas import UserUpdate

client = TestClient(app)

PASSWORD = "TestPass1"


# ── helpers ──────────────────────────────────────────────────────────────────

def _tag() -> str:
    return uuid.uuid4().hex[:10]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _login(email: str) -> str:
    r = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _make_candidate():
    """Register a candidate through the public API. Returns (token, user_id, email)."""
    tag = _tag()
    email = f"cand_{tag}@easyrecruit.dev"
    r = client.post("/api/v1/auth/register", json={
        "email": email,
        "username": f"cand_{tag}",
        "password": PASSWORD,
        "full_name": "Priv Test Candidate",
        "role": "candidate",
    })
    assert r.status_code == 201, r.text
    return _login(email), r.json()["id"], email


def _make_admin():
    """Admins cannot self-register, so provision one directly in the DB.
    Returns (token, user_id)."""
    tag = _tag()
    email = f"admin_{tag}@easyrecruit.dev"
    user_id = db.insert(
        "INSERT INTO users (email, username, password_hash, full_name, role, approval_status) "
        "VALUES (?, ?, ?, ?, 'admin', 'approved')",
        (email, f"admin_{tag}", hash_password(PASSWORD), "Priv Test Admin"),
    )
    return _login(email), user_id


def _db_user(user_id: int) -> dict:
    return db.fetch_one(
        "SELECT role, is_active, company_name, full_name, email, username "
        "FROM users WHERE id = ?",
        (user_id,),
    )


# ── self-service endpoint must not touch privileged fields ───────────────────

class TestSelfServiceCannotEscalate:

    @pytest.mark.parametrize("target_role", ["admin", "company_admin", "recruiter"])
    def test_candidate_cannot_change_own_role(self, target_role):
        token, uid, _ = _make_candidate()
        r = client.put("/api/v1/auth/me", headers=_auth(token), json={"role": target_role})
        assert r.status_code < 500, r.text
        assert _db_user(uid)["role"] == "candidate"
        me = client.get("/api/v1/auth/me", headers=_auth(token))
        assert me.status_code == 200
        assert me.json()["role"] == "candidate"

    def test_failed_self_promotion_grants_no_admin_access(self):
        token, _, _ = _make_candidate()
        client.put("/api/v1/auth/me", headers=_auth(token), json={"role": "admin"})
        assert client.get("/api/v1/auth/admin/users", headers=_auth(token)).status_code == 403
        assert client.get("/api/v1/auth/admin/stats", headers=_auth(token)).status_code == 403
        assert client.get("/api/v1/auth/company-admin/team", headers=_auth(token)).status_code == 403

    def test_candidate_cannot_deactivate_own_account(self):
        token, uid, _ = _make_candidate()
        r = client.put("/api/v1/auth/me", headers=_auth(token), json={"is_active": False})
        assert r.status_code < 500, r.text
        assert _db_user(uid)["is_active"] == 1
        # token must still work — a deactivated account would be rejected (401)
        assert client.get("/api/v1/auth/me", headers=_auth(token)).status_code == 200

    def test_candidate_cannot_change_company_assignment(self):
        token, uid, _ = _make_candidate()
        before = _db_user(uid)["company_name"]
        r = client.put("/api/v1/auth/me", headers=_auth(token), json={"company_name": "Some Other Corp"})
        assert r.status_code < 500, r.text
        assert _db_user(uid)["company_name"] == before

    def test_mixed_payload_applies_only_permitted_fields(self):
        """Normal profile editing keeps working even if privileged fields ride along."""
        token, uid, _ = _make_candidate()
        r = client.put("/api/v1/auth/me", headers=_auth(token), json={
            "full_name": "Renamed Candidate",
            "role": "admin",
            "is_active": False,
            "company_name": "Evil Corp",
        })
        assert r.status_code == 200, r.text
        row = _db_user(uid)
        assert row["full_name"] == "Renamed Candidate"
        assert row["role"] == "candidate"
        assert row["is_active"] == 1
        assert row["company_name"] is None

    def test_admin_cannot_alter_own_privileges_via_self_service(self):
        token, uid = _make_admin()
        client.put("/api/v1/auth/me", headers=_auth(token), json={"role": "candidate", "is_active": False})
        row = _db_user(uid)
        assert row["role"] == "admin"
        assert row["is_active"] == 1

    def test_normal_profile_edit_still_works(self):
        token, uid, _ = _make_candidate()
        tag = _tag()
        r = client.put("/api/v1/auth/me", headers=_auth(token), json={
            "full_name": "New Display Name",
            "username": f"renamed_{tag}",
            "email": f"renamed_{tag}@easyrecruit.dev",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["full_name"] == "New Display Name"
        assert body["username"] == f"renamed_{tag}"
        assert body["email"] == f"renamed_{tag}@easyrecruit.dev"

    def test_empty_update_is_still_a_noop(self):
        token, uid, _ = _make_candidate()
        r = client.put("/api/v1/auth/me", headers=_auth(token), json={})
        assert r.status_code == 200, r.text
        assert r.json()["role"] == "candidate"

    def test_self_service_schema_exposes_only_profile_fields(self):
        """Guard: adding a field to UserUpdate must be a deliberate, reviewed change."""
        assert set(UserUpdate.model_fields) == {"email", "username", "full_name"}


# ── privileged changes must stay admin-only (pre-existing behaviour) ─────────

class TestPrivilegedEndpointsStayAdminOnly:

    def test_candidate_cannot_use_admin_role_endpoint(self):
        token, uid, _ = _make_candidate()
        r = client.patch(f"/api/v1/auth/admin/users/{uid}/role",
                         headers=_auth(token), json={"role": "admin"})
        assert r.status_code == 403
        assert _db_user(uid)["role"] == "candidate"

    def test_candidate_cannot_use_admin_toggle_endpoint(self):
        token, uid, _ = _make_candidate()
        r = client.patch(f"/api/v1/auth/admin/users/{uid}/toggle", headers=_auth(token))
        assert r.status_code == 403
        assert _db_user(uid)["is_active"] == 1

    def test_unauthenticated_cannot_use_admin_endpoints(self):
        assert client.patch("/api/v1/auth/admin/users/1/role", json={"role": "admin"}).status_code == 401
        assert client.patch("/api/v1/auth/admin/users/1/toggle").status_code == 401

    def test_admin_can_still_change_role_and_toggle_active(self):
        admin_token, _ = _make_admin()
        _, victim_id, _ = _make_candidate()

        r = client.patch(f"/api/v1/auth/admin/users/{victim_id}/role",
                         headers=_auth(admin_token), json={"role": "recruiter"})
        assert r.status_code == 200, r.text
        assert _db_user(victim_id)["role"] == "recruiter"

        r = client.patch(f"/api/v1/auth/admin/users/{victim_id}/toggle", headers=_auth(admin_token))
        assert r.status_code == 200, r.text
        assert _db_user(victim_id)["is_active"] == 0

        r = client.patch(f"/api/v1/auth/admin/users/{victim_id}/toggle", headers=_auth(admin_token))
        assert r.status_code == 200, r.text
        assert _db_user(victim_id)["is_active"] == 1
