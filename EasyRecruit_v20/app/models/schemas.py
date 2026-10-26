"""
EasyRecruit ATS 3.0 — Pydantic Schemas
All request/response models with enhanced validation.
"""
from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator


# ════════════════════════════════════════════════════════════════════════════
# Enumerations
# ════════════════════════════════════════════════════════════════════════════

class UserRole(str, Enum):
    RECRUITER       = "recruiter"
    HIRING_MANAGER  = "hiring_manager"
    ADMIN           = "admin"
    COMPANY_ADMIN   = "company_admin"
    CANDIDATE       = "candidate"


class JobType(str, Enum):
    FULL_TIME  = "full_time"
    PART_TIME  = "part_time"
    CONTRACT   = "contract"
    INTERNSHIP = "internship"
    REMOTE     = "remote"
    HYBRID     = "hybrid"


class DatasetCategory(str, Enum):
    SKILLS            = "skills"
    KEYWORDS          = "keywords"
    JOB_TITLES        = "job_titles"
    INDUSTRY_SPECIFIC = "industry_specific"


class Priority(str, Enum):
    HIGH   = "high"
    MEDIUM = "medium"
    LOW    = "low"


# ════════════════════════════════════════════════════════════════════════════
# User models
# ════════════════════════════════════════════════════════════════════════════

class UserBase(BaseModel):
    email:        EmailStr
    username:     str        = Field(..., min_length=3, max_length=50, pattern=r"^[a-zA-Z0-9_-]+$")
    full_name:    Optional[str] = Field(None, max_length=200)
    company_name: Optional[str] = Field(None, max_length=200)
    role:         UserRole   = UserRole.RECRUITER


class UserCreate(UserBase):
    password: str = Field(..., min_length=8, max_length=128)

    @model_validator(mode="before")
    @classmethod
    def normalise_registration_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Accept "name" as alias for "full_name"
            if "name" in data and "full_name" not in data:
                data["full_name"] = data.pop("name")
            # Auto-generate username from e-mail if not provided
            if "username" not in data and "email" in data:
                base = re.sub(r"[^a-zA-Z0-9_]", "", data["email"].split("@")[0])[:20]
                data["username"] = base or "user"
        return data

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit")
        if not any(c.isalpha() for c in v):
            raise ValueError("Password must contain at least one letter")
        return v

    @model_validator(mode="after")
    def recruiter_requires_company(self) -> "UserCreate":
        if self.role == UserRole.RECRUITER and not (self.company_name or "").strip():
            raise ValueError("Recruiters must select the company they're recruiting for.")
        return self


class UserUpdate(BaseModel):
    # Self-service profile fields ONLY (used by PUT /auth/me). Privileged
    # fields — role, is_active, company_name (tenant scope) — must never be
    # added here; they are changed through the admin-only endpoints.
    email:        Optional[EmailStr] = None
    username:     Optional[str]      = Field(None, min_length=3, max_length=50)
    full_name:    Optional[str]      = Field(None, max_length=200)


class UserResponse(UserBase):
    id:             int
    is_active:      bool
    approval_status: str = "approved"
    login_count:    int        = 0
    last_login_at:  Optional[datetime] = None
    created_at:     datetime
    updated_at:     datetime
    photo_url:      Optional[str] = None
    date_of_birth:  Optional[str] = None
    gender:         Optional[str] = None
    phone:          Optional[str] = None
    address:        Optional[str] = None
    profile_complete: bool = False

    @model_validator(mode="before")
    @classmethod
    def compute_profile_complete(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            # DB rows use the column name `photo_path`; map it onto this
            # model's `photo_url` here (input side only) rather than via a
            # Pydantic `alias`, which would also rename the field back to
            # `photo_path` on JSON *output* -- exactly the bug that left
            # the frontend reading `undefined` for a user's own photo on
            # every fresh login, even though one was on file.
            if "photo_url" not in data and "photo_path" in data:
                data["photo_url"] = data.get("photo_path")
            data["profile_complete"] = bool(
                data.get("full_name") and data.get("date_of_birth")
                and data.get("phone") and data.get("address")
            )
        return data

    class Config:
        from_attributes = True


class UserLogin(BaseModel):
    email:    EmailStr
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password:     str = Field(..., min_length=8)

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if not any(c.isdigit() for c in v):
            raise ValueError("New password must contain at least one digit")
        return v


class TokenResponse(BaseModel):
    access_token:  str
    token_type:    str = "bearer"
    expires_in:    int  # seconds
    user:          UserResponse


# ════════════════════════════════════════════════════════════════════════════
# Job description models
# ════════════════════════════════════════════════════════════════════════════

class JobDescriptionBase(BaseModel):
    title:            str            = Field(..., min_length=1, max_length=200)
    company:          Optional[str]  = Field(None, max_length=200)
    department:       Optional[str]  = Field(None, max_length=200)
    location:         Optional[str]  = Field(None, max_length=200)
    job_type:         JobType        = JobType.FULL_TIME
    description:      str
    required_skills:  Optional[List[str]] = None
    preferred_skills: Optional[List[str]] = None
    min_experience:   Optional[int]  = Field(None, ge=0, le=50)
    max_experience:   Optional[int]  = Field(None, ge=0, le=50)
    education_level:  Optional[str]  = None
    salary_min:       Optional[float] = Field(None, ge=0)
    salary_max:       Optional[float] = Field(None, ge=0)

    @model_validator(mode="after")
    def validate_experience_range(self) -> "JobDescriptionBase":
        if (
            self.min_experience is not None
            and self.max_experience is not None
            and self.min_experience > self.max_experience
        ):
            raise ValueError("min_experience must be <= max_experience")
        return self

    @model_validator(mode="after")
    def validate_salary_range(self) -> "JobDescriptionBase":
        if (
            self.salary_min is not None
            and self.salary_max is not None
            and self.salary_min > self.salary_max
        ):
            raise ValueError("salary_min must be <= salary_max")
        return self


class JobDescriptionCreate(JobDescriptionBase):
    pass


class JobDescriptionUpdate(BaseModel):
    title:            Optional[str]        = Field(None, min_length=1, max_length=200)
    company:          Optional[str]        = None
    department:       Optional[str]        = None
    location:         Optional[str]        = None
    job_type:         Optional[JobType]    = None
    description:      Optional[str]        = None
    required_skills:  Optional[List[str]]  = None
    preferred_skills: Optional[List[str]]  = None
    min_experience:   Optional[int]        = Field(None, ge=0)
    max_experience:   Optional[int]        = Field(None, ge=0)
    education_level:  Optional[str]        = None
    salary_min:       Optional[float]      = None
    salary_max:       Optional[float]      = None
    is_active:        Optional[bool]       = None


class JobDescriptionResponse(JobDescriptionBase):
    id:         int
    user_id:    int
    is_active:  bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ════════════════════════════════════════════════════════════════════════════
# Analysis models
# ════════════════════════════════════════════════════════════════════════════

class SkillMatch(BaseModel):
    skill:      str
    category:   str
    matched:    bool
    confidence: float = Field(..., ge=0.0, le=1.0)


class KeywordMatch(BaseModel):
    keyword: str
    matched: bool
    score:   float


class Recommendation(BaseModel):
    category:   str
    suggestion: str
    priority:   Priority


class ResumeAnalysisFullResponse(BaseModel):
    analysis_id:          int
    filename:             str
    extracted_text_length: int
    candidate_name:        Optional[str] = None
    candidate_email:       Optional[str] = None
    overall_score:         float
    keyword_match_score:   float
    skill_match_score:     float
    structure_score:       float
    semantic_similarity:   Optional[float] = None
    experience_score:      float = 0.0
    education_score:       float = 0.0
    # DEPRECATED alias of extraction_confidence, kept so existing clients keep working. It is a parse-completeness
    # heuristic, NOT a measured accuracy; it no longer defaults to an invented 85.0 when the scorer supplies none.
    accuracy_estimate:     Optional[float] = None
    extraction_confidence: Optional[float] = None
    # How the score was produced: weights, per-dimension points, adjustments, caps, notes, warnings, limitations.
    scoring_explanation:   Optional[Dict[str, Any]] = None
    nlp_analysis:          Dict[str, Any]
    breakdown:             Dict[str, Any]
    status:                str
    created_at:            Optional[datetime] = None
    # Additive (Task 5): whether the result was stored, and why not when storing was requested but failed.
    saved:                 Optional[bool] = None
    save_error:            Optional[str] = None


# ════════════════════════════════════════════════════════════════════════════
# Custom dataset models
# ════════════════════════════════════════════════════════════════════════════

class CustomDatasetBase(BaseModel):
    name:        str              = Field(..., min_length=1, max_length=100)
    description: Optional[str]   = None
    category:    DatasetCategory
    skills:      Optional[List[str]] = None
    keywords:    Optional[List[str]] = None


class CustomDatasetCreate(CustomDatasetBase):
    pass


class CustomDatasetUpdate(BaseModel):
    name:        Optional[str]              = Field(None, min_length=1, max_length=100)
    description: Optional[str]             = None
    category:    Optional[DatasetCategory] = None
    skills:      Optional[List[str]]       = None
    keywords:    Optional[List[str]]       = None
    is_active:   Optional[bool]            = None


class CustomDatasetResponse(CustomDatasetBase):
    id:         int
    user_id:    int
    is_active:  bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ════════════════════════════════════════════════════════════════════════════
# Utility / generic response models
# ════════════════════════════════════════════════════════════════════════════

class APIResponse(BaseModel):
    success: bool
    message: str
    data:    Optional[Any] = None


class PaginatedResponse(BaseModel):
    items:       List[Any]
    total:       int
    page:        int
    page_size:   int
    total_pages: int
    has_next:    bool
    has_prev:    bool

    @model_validator(mode="after")
    def compute_flags(self) -> "PaginatedResponse":
        self.has_next = self.page < self.total_pages
        self.has_prev = self.page > 1
        return self


class HealthCheckResponse(BaseModel):
    status:    str
    version:   str
    services:  Dict[str, str]
    database:  str
    timestamp: datetime


class AnalysisHistoryResponse(BaseModel):
    id:             int
    user_id:        int
    analysis_type:  str
    description:    Optional[str]
    result_summary: Optional[str]
    created_at:     datetime

    class Config:
        from_attributes = True


class AnalysisStatsResponse(BaseModel):
    total_analyses:         int
    average_score:          float
    highest_score:          float
    lowest_score:           float
    score_distribution:     Dict[str, int]
    recent_activity_7_days: int
    total_jobs:             int
    total_datasets:         int
    top_skills_found:       List[str]
