"""
EasyRecruit ATS 3.0 — Job Description Routes
Improvements: department, location, job_type, salary fields, search/filter, pagination.
"""
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.auth_routes import get_current_user
from app.config import settings
from app.db.database import db
from app.models.schemas import (
    APIResponse, JobDescriptionCreate, JobDescriptionResponse,
    JobDescriptionUpdate, PaginatedResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/jobs", tags=["Job Descriptions"])

# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

_JSON_FIELDS = {"required_skills", "preferred_skills"}


def _deserialise(job: Optional[dict]) -> Optional[dict]:
    """Parse JSON-stored list fields back into Python lists."""
    if job is None:
        return None
    result = dict(job)
    for field in _JSON_FIELDS:
        raw = result.get(field)
        if isinstance(raw, str):
            try:
                result[field] = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                result[field] = []
    return result


def get_job_by_id(job_id: int) -> Optional[dict]:
    row = db.fetch_one(
        "SELECT * FROM job_descriptions WHERE id = ? AND is_active = 1", (job_id,)
    )
    return _deserialise(row)


def _assert_ownership(job: dict, user_id: int, action: str = "access"):
    if job["user_id"] != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Not authorised to {action} this job.",
        )


# ────────────────────────────────────────────────────────────────────────────
# Routes
# ────────────────────────────────────────────────────────────────────────────

@router.post("/", response_model=JobDescriptionResponse, status_code=status.HTTP_201_CREATED)
async def create_job(
    job_data: JobDescriptionCreate,
    current_user: dict = Depends(get_current_user),
):
    """Create a new job description."""
    try:
        job_id = db.insert(
            """INSERT INTO job_descriptions
               (user_id, title, company, department, location, job_type,
                description, required_skills, preferred_skills,
                min_experience, max_experience, education_level,
                salary_min, salary_max)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                current_user["id"],
                job_data.title,
                job_data.company,
                job_data.department,
                job_data.location,
                job_data.job_type.value,
                job_data.description,
                json.dumps(job_data.required_skills) if job_data.required_skills else None,
                json.dumps(job_data.preferred_skills) if job_data.preferred_skills else None,
                job_data.min_experience,
                job_data.max_experience,
                job_data.education_level,
                job_data.salary_min,
                job_data.salary_max,
            ),
        )
        logger.info(f"Job created: '{job_data.title}' by user {current_user['id']}")
        return get_job_by_id(job_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Job creation error for user {current_user['id']}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error creating job description. Please try again.")


@router.get("/", response_model=PaginatedResponse)
async def list_jobs(
    page:     int            = Query(1, ge=1),
    per_page: int            = Query(10, ge=1, le=100),
    search:   Optional[str]  = Query(None, description="Search title or company"),
    job_type: Optional[str]  = Query(None),
    current_user: dict = Depends(get_current_user),
):
    """List jobs for the current user with optional search/filter."""
    base_cond = "user_id = ? AND is_active = 1"
    params: list = [current_user["id"]]

    if search:
        base_cond += " AND (title LIKE ? OR company LIKE ?)"
        like = f"%{search}%"
        params += [like, like]

    if job_type:
        base_cond += " AND job_type = ?"
        params.append(job_type)

    total_row = db.fetch_one(
        f"SELECT COUNT(*) as total FROM job_descriptions WHERE {base_cond}",
        tuple(params),
    )
    total = total_row["total"] if total_row else 0

    offset = (page - 1) * per_page
    rows = db.fetch_all(
        f"""SELECT * FROM job_descriptions
            WHERE {base_cond}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?""",
        tuple(params + [per_page, offset]),
    )

    items = [_deserialise(r) for r in rows]
    total_pages = max(1, (total + per_page - 1) // per_page)

    return {
        "items":       items,
        "total":       total,
        "page":        page,
        "page_size":   per_page,
        "total_pages": total_pages,
        "has_next":    page < total_pages,
        "has_prev":    page > 1,
    }


@router.get("/{job_id}", response_model=JobDescriptionResponse)
async def get_job(
    job_id: int,
    current_user: dict = Depends(get_current_user),
):
    """Retrieve a specific job description."""
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    _assert_ownership(job, current_user["id"])
    return job


@router.put("/{job_id}", response_model=JobDescriptionResponse)
async def update_job(
    job_id:     int,
    job_update: JobDescriptionUpdate,
    current_user: dict = Depends(get_current_user),
):
    """Update a job description (partial update supported)."""
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    _assert_ownership(job, current_user["id"], "update")

    fields, values = [], []

    plain_fields = {
        "title":           job_update.title,
        "company":         job_update.company,
        "department":      job_update.department,
        "location":        job_update.location,
        "description":     job_update.description,
        "min_experience":  job_update.min_experience,
        "max_experience":  job_update.max_experience,
        "education_level": job_update.education_level,
        "salary_min":      job_update.salary_min,
        "salary_max":      job_update.salary_max,
    }
    for col, val in plain_fields.items():
        if val is not None:
            fields.append(f"{col} = ?")
            values.append(val)

    if job_update.job_type is not None:
        fields.append("job_type = ?")
        values.append(job_update.job_type.value)

    for col in ("required_skills", "preferred_skills"):
        val = getattr(job_update, col)
        if val is not None:
            fields.append(f"{col} = ?")
            values.append(json.dumps(val))

    if job_update.is_active is not None:
        fields.append("is_active = ?")
        values.append(1 if job_update.is_active else 0)

    if not fields:
        return job

    fields.append("updated_at = CURRENT_TIMESTAMP")
    values.append(job_id)

    try:
        db.execute(
            f"UPDATE job_descriptions SET {', '.join(fields)} WHERE id = ?",
            tuple(values),
        )
        return get_job_by_id(job_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Job update error for job {job_id}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error updating job description. Please try again.")


@router.delete("/{job_id}", response_model=APIResponse)
async def delete_job(
    job_id: int,
    current_user: dict = Depends(get_current_user),
):
    """Soft-delete a job description."""
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    _assert_ownership(job, current_user["id"], "delete")

    db.execute(
        "UPDATE job_descriptions SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (job_id,),
    )
    logger.info(f"Job soft-deleted: {job_id} by user {current_user['id']}")
    return {"success": True, "message": "Job deleted successfully.", "data": None}


@router.get("/{job_id}/config")
async def job_config(
    job_id: int,
    current_user: dict = Depends(get_current_user),
):
    """Return structured analysis config for a job (used when uploading resume against a specific job)."""
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    _assert_ownership(job, current_user["id"])

    return {
        "job_id":           job_id,
        "title":            job["title"],
        "company":          job["company"],
        "description":      job["description"],
        "required_skills":  job["required_skills"] or [],
        "preferred_skills": job["preferred_skills"] or [],
        "experience_range": {
            "min": job["min_experience"],
            "max": job["max_experience"],
        },
        "education_level":  job["education_level"],
        "job_type":         job["job_type"],
    }
