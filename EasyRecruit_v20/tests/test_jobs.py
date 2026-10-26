"""
EasyRecruit ATS 3.0 — Job Description CRUD Tests
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

_token   = ""
_job_id  = None

JOB_PAYLOAD = {
    "title":           "Senior Python Engineer",
    "company":         "Test Corp",
    "department":      "Engineering",
    "location":        "Remote",
    "job_type":        "remote",
    "description":     "We need a great Python engineer with FastAPI, PostgreSQL and AWS experience.",
    "required_skills": ["python", "fastapi", "postgresql"],
    "min_experience":  3,
    "max_experience":  8,
}


def _login():
    global _token
    # Ensure user exists
    client.post("/api/v1/auth/register", json={
        "email":"jobtest@easyrecruit.dev","username":"jobtester","password":"JobTest1","role":"candidate"
    })
    r = client.post("/api/v1/auth/login",
                    json={"email":"jobtest@easyrecruit.dev","password":"JobTest1"})
    _token = r.json().get("access_token","")


class TestJobs:
    @classmethod
    def setup_class(cls):
        _login()

    def _auth(self):
        return {"Authorization": f"Bearer {_token}"}

    def test_create_job(self):
        global _job_id
        r = client.post("/api/v1/jobs/", json=JOB_PAYLOAD, headers=self._auth())
        assert r.status_code == 201
        data = r.json()
        assert data["title"] == JOB_PAYLOAD["title"]
        _job_id = data["id"]

    def test_list_jobs(self):
        r = client.get("/api/v1/jobs/", headers=self._auth())
        assert r.status_code == 200
        assert "items" in r.json()

    def test_get_job(self):
        r = client.get(f"/api/v1/jobs/{_job_id}", headers=self._auth())
        assert r.status_code == 200
        assert r.json()["id"] == _job_id

    def test_search_jobs(self):
        r = client.get("/api/v1/jobs/?search=Python", headers=self._auth())
        assert r.status_code == 200

    def test_update_job(self):
        r = client.put(f"/api/v1/jobs/{_job_id}",
                       json={"title":"Updated Title"},
                       headers=self._auth())
        assert r.status_code == 200
        assert r.json()["title"] == "Updated Title"

    def test_get_job_config(self):
        r = client.get(f"/api/v1/jobs/{_job_id}/config", headers=self._auth())
        assert r.status_code == 200
        assert "required_skills" in r.json()

    def test_unauthorized_access(self):
        r = client.get(f"/api/v1/jobs/{_job_id}")
        assert r.status_code == 401

    def test_delete_job(self):
        r = client.delete(f"/api/v1/jobs/{_job_id}", headers=self._auth())
        assert r.status_code == 200

    def test_deleted_job_not_found(self):
        r = client.get(f"/api/v1/jobs/{_job_id}", headers=self._auth())
        assert r.status_code == 404
