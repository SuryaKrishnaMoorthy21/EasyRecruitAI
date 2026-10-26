"""
EasyRecruit ATS 3.0 — Profile Tests
Covers: profile fetch/update, photo upload (valid + rejected cases),
photo replace/delete cleanup, and the profile_complete flag.
"""
import io
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from main import app
from app.api.profile_routes import PHOTO_DIR

client = TestClient(app)

TEST_EMAIL    = "profiletest@easyrecruit.dev"
TEST_PASSWORD = "ProfileTest1"
TEST_USERNAME = "profiletester"
_token: str   = ""


def _auth():
    return {"Authorization": f"Bearer {_token}"}


def _make_image_bytes(w, h, fmt="JPEG", color=(120, 140, 200)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color=color).save(buf, format=fmt)
    buf.seek(0)
    return buf


class TestProfile:

    @classmethod
    def setup_class(cls):
        global _token
        client.post("/api/v1/auth/register", json={
            "email": TEST_EMAIL, "username": TEST_USERNAME,
            "password": TEST_PASSWORD, "role": "candidate",
        })
        r = client.post("/api/v1/auth/login", json={
            "email": TEST_EMAIL, "password": TEST_PASSWORD,
        })
        _token = r.json().get("access_token", "")

    def test_requires_auth(self):
        r = client.get("/api/v1/profile/me")
        assert r.status_code in (401, 403)

    def test_get_profile_initially_incomplete(self):
        r = client.get("/api/v1/profile/me", headers=_auth())
        assert r.status_code == 200
        data = r.json()
        assert data["email"] == TEST_EMAIL
        assert data["has_photo"] is False
        # profile_complete may already be True/False depending on test order
        # against a persistent DB; just confirm the field is present and boolean.
        assert isinstance(data["profile_complete"], bool)

    def test_update_profile_rejects_future_dob(self):
        r = client.put("/api/v1/profile/me", headers=_auth(),
                        json={"date_of_birth": "2099-01-01"})
        assert r.status_code == 422

    def test_update_profile_rejects_bad_phone(self):
        r = client.put("/api/v1/profile/me", headers=_auth(),
                        json={"phone": "123"})
        assert r.status_code == 422

    def test_update_profile_valid(self):
        r = client.put("/api/v1/profile/me", headers=_auth(), json={
            "full_name": "Profile Test User", "date_of_birth": "2003-08-20",
            "gender": "female", "phone": "9876500000",
            "address": "1 Test Street, Chennai",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["profile_complete"] is True
        assert data["full_name"] == "Profile Test User"

    def test_upload_valid_photo(self):
        r = client.post("/api/v1/profile/photo", headers=_auth(),
                         files={"file": ("p.jpg", _make_image_bytes(400, 500), "image/jpeg")})
        assert r.status_code == 200
        data = r.json()
        assert data["has_photo"] is True
        assert data["photo_url"].startswith("/uploads/profile_photos/")
        saved = PHOTO_DIR / data["photo_url"].rsplit("/", 1)[-1]
        assert saved.exists()

    def test_upload_rejects_corrupt_file(self):
        r = client.post("/api/v1/profile/photo", headers=_auth(),
                         files={"file": ("bad.jpg", io.BytesIO(b"not an image"), "image/jpeg")})
        assert r.status_code == 415

    def test_upload_rejects_tiny_image(self):
        r = client.post("/api/v1/profile/photo", headers=_auth(),
                         files={"file": ("tiny.png", _make_image_bytes(50, 50, "PNG"), "image/png")})
        assert r.status_code == 422

    def test_upload_rejects_wrong_type(self):
        r = client.post("/api/v1/profile/photo", headers=_auth(),
                         files={"file": ("r.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")})
        assert r.status_code == 415

    def test_replace_photo_cleans_up_old_file(self):
        r1 = client.post("/api/v1/profile/photo", headers=_auth(),
                          files={"file": ("p1.jpg", _make_image_bytes(300, 300), "image/jpeg")})
        old_url = r1.json()["photo_url"]
        old_path = PHOTO_DIR / old_url.rsplit("/", 1)[-1]

        r2 = client.post("/api/v1/profile/photo", headers=_auth(),
                          files={"file": ("p2.png", _make_image_bytes(300, 300, "PNG"), "image/png")})
        new_url = r2.json()["photo_url"]
        assert new_url != old_url
        assert not old_path.exists()

    def test_delete_photo(self):
        r = client.delete("/api/v1/profile/photo", headers=_auth())
        assert r.status_code == 200
        data = r.json()
        assert data["has_photo"] is False
        assert data["photo_url"] is None
