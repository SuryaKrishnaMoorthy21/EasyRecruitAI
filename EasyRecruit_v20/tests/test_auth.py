"""
EasyRecruit ATS 3.0 — Auth Tests
Covers: register, login, token auth, profile update, change-password, logout.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

TEST_EMAIL    = "test3@easyrecruit.dev"
TEST_PASSWORD = "TestPass1"
TEST_USERNAME = "testuser3"
_token: str   = ""


class TestAuth:

    def test_health(self):
        r = client.get("/api/v1/health")
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"

    def test_register_new_user(self):
        r = client.post("/api/v1/auth/register", json={
            "email":      TEST_EMAIL,
            "username":   TEST_USERNAME,
            "password":   TEST_PASSWORD,
            "full_name":  "Test User v3",
            "role":       "candidate",
        })
        assert r.status_code in (201, 400)   # 400 if already exists

    def test_register_invalid_email(self):
        r = client.post("/api/v1/auth/register", json={
            "email": "not-an-email", "username": "u2", "password": "Pass123"
        })
        assert r.status_code == 422

    def test_register_weak_password(self):
        r = client.post("/api/v1/auth/register", json={
            "email": "weak@test.com", "username": "weakuser", "password": "letters"
        })
        assert r.status_code == 422   # no digit → validation error

    def test_register_short_password(self):
        r = client.post("/api/v1/auth/register", json={
            "email": "short@test.com", "username": "shortpw", "password": "Ab1"
        })
        assert r.status_code == 422

    def test_login_valid(self):
        global _token
        r = client.post("/api/v1/auth/login", json={
            "email": TEST_EMAIL, "password": TEST_PASSWORD
        })
        assert r.status_code == 200
        data = r.json()
        assert "access_token" in data
        assert "expires_in"   in data
        assert data["token_type"] == "bearer"
        _token = data["access_token"]

    def test_login_wrong_password(self):
        r = client.post("/api/v1/auth/login", json={
            "email": TEST_EMAIL, "password": "WrongPassword9"
        })
        assert r.status_code == 401

    def test_login_unknown_email(self):
        r = client.post("/api/v1/auth/login", json={
            "email": "nobody@nope.com", "password": "Pass123"
        })
        assert r.status_code == 401

    def test_get_me(self):
        r = client.get("/api/v1/auth/me",
                       headers={"Authorization": f"Bearer {_token}"})
        assert r.status_code == 200
        assert r.json()["email"] == TEST_EMAIL

    def test_get_me_no_token(self):
        r = client.get("/api/v1/auth/me")
        assert r.status_code == 401

    def test_update_profile(self):
        r = client.put("/api/v1/auth/me",
                       json={"full_name": "Updated User v3"},
                       headers={"Authorization": f"Bearer {_token}"})
        assert r.status_code == 200
        assert r.json()["full_name"] == "Updated User v3"

    def test_change_password(self):
        new_pw = "NewPass1"
        r = client.post("/api/v1/auth/change-password",
                        json={"current_password": TEST_PASSWORD, "new_password": new_pw},
                        headers={"Authorization": f"Bearer {_token}"})
        assert r.status_code == 200
        # Restore original password
        client.post("/api/v1/auth/change-password",
                    json={"current_password": new_pw, "new_password": TEST_PASSWORD},
                    headers={"Authorization": f"Bearer {_token}"})

    def test_change_password_wrong_current(self):
        r = client.post("/api/v1/auth/change-password",
                        json={"current_password": "WrongCurrent9", "new_password": "NewPass9"},
                        headers={"Authorization": f"Bearer {_token}"})
        assert r.status_code == 400

    def test_logout(self):
        r = client.post("/api/v1/auth/logout",
                        headers={"Authorization": f"Bearer {_token}"})
        assert r.status_code == 200

    def test_token_blacklisted_after_logout(self):
        # After logout, token should be rejected
        r = client.get("/api/v1/auth/me",
                       headers={"Authorization": f"Bearer {_token}"})
        assert r.status_code == 401
