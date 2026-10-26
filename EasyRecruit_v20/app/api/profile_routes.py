"""
EasyRecruit ATS 3.0 — Job Seeker Profile Routes
Profile fields (name, DOB, gender, phone, address) and a passport-style
profile photo, stored on disk with a resized/validated copy and served as
a static file so it can be dropped straight into an <img> tag anywhere in
the UI (navbar, candidate views, application flow).
"""
import logging
import re
import secrets
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field, field_validator

from app.api.auth_routes import get_current_user
from app.config import settings, BASE_DIR
from app.db.database import db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/profile", tags=["Profile"])

# ────────────────────────────────────────────────────────────────────────────
# Storage setup
# ────────────────────────────────────────────────────────────────────────────

PHOTO_DIR = BASE_DIR / "app" / "data" / "uploads" / "profile_photos"
PHOTO_DIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_BYTES = 5 * 1024 * 1024   # 5 MB before resizing
MAX_PHOTO_DIMENSION = 800            # longer side, after resize
MIN_PHOTO_DIMENSION = 150            # reject anything smaller than this
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}


# ────────────────────────────────────────────────────────────────────────────
# Schemas (kept local to this route file since they're only used here)
# ────────────────────────────────────────────────────────────────────────────

class ProfileUpdate(BaseModel):
    full_name:     Optional[str] = Field(None, max_length=200)
    date_of_birth: Optional[str] = Field(None, description="YYYY-MM-DD")
    gender:        Optional[str] = Field(None, max_length=30)
    phone:         Optional[str] = Field(None, max_length=20)
    address:       Optional[str] = Field(None, max_length=500)

    @field_validator("date_of_birth")
    @classmethod
    def valid_dob(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return v
        try:
            parsed = datetime.strptime(v, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("Date of birth must be in YYYY-MM-DD format.")
        if parsed > date.today():
            raise ValueError("Date of birth can't be in the future.")
        if parsed.year < 1930:
            raise ValueError("Please check the date of birth.")
        return v

    @field_validator("phone")
    @classmethod
    def valid_phone(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return v
        digits = re.sub(r"[^\d]", "", v)
        if not (7 <= len(digits) <= 15):
            raise ValueError("Enter a valid phone number.")
        return v.strip()

    @field_validator("gender")
    @classmethod
    def valid_gender(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return v
        allowed = {"male", "female", "other", "prefer_not_to_say"}
        if v.lower() not in allowed:
            raise ValueError("Gender must be one of: male, female, other, prefer_not_to_say.")
        return v.lower()


def _profile_payload(row: dict) -> dict:
    """Shape a users-table row into the profile response the frontend uses."""
    required_filled = bool(
        row.get("full_name") and row.get("date_of_birth")
        and row.get("phone") and row.get("address")
    )
    return {
        "id":                 row["id"],
        "user_id":            row["id"],
        "email":              row["email"],
        "username":           row["username"],
        "full_name":          row.get("full_name"),
        "role":               row.get("role"),
        "date_of_birth":      row.get("date_of_birth"),
        "gender":             row.get("gender"),
        "phone":              row.get("phone"),
        "address":            row.get("address"),
        "photo_url":          row.get("photo_path"),
        "profile_complete":   required_filled,
        "has_photo":          bool(row.get("photo_path")),
        "profile_updated_at": row.get("profile_updated_at"),
        "created_at":         row.get("created_at"),
    }


def _get_user_row(user_id: int) -> dict:
    row = db.fetch_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if not row:
        raise HTTPException(status_code=404, detail="User not found.")
    return row


# ────────────────────────────────────────────────────────────────────────────
# Routes
# ────────────────────────────────────────────────────────────────────────────

@router.get("/me")
async def get_my_profile(current_user: dict = Depends(get_current_user)):
    """Full profile for the signed-in user — fields plus photo URL and a
    profile_complete flag the frontend uses to decide whether to prompt."""
    return _profile_payload(_get_user_row(current_user["id"]))


@router.put("/me")
async def update_my_profile(
    payload: ProfileUpdate,
    current_user: dict = Depends(get_current_user),
):
    """Partial update — only fields provided are changed."""
    fields, values = [], []
    for key, value in payload.model_dump(exclude_unset=True).items():
        fields.append(f"{key} = ?")
        values.append(value)
    if not fields:
        return _profile_payload(_get_user_row(current_user["id"]))

    fields.append("profile_updated_at = CURRENT_TIMESTAMP")
    values.append(current_user["id"])
    try:
        db.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", tuple(values))
    except Exception as exc:
        logger.error(f"Profile update failed for user {current_user['id']}: {exc}")
        raise HTTPException(status_code=500, detail="Could not save profile. Please try again.")

    logger.info(f"Profile updated: user {current_user['id']}")
    return _profile_payload(_get_user_row(current_user["id"]))


@router.post("/photo")
async def upload_profile_photo(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    """Upload (or replace) the user's profile photo. Validated and resized
    server-side with Pillow -- this never trusts the browser's declared
    content-type alone, since that's trivially spoofable."""
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail="Please upload a JPG, PNG, or WEBP image.",
        )

    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail="Image is too large. Please use a file under 5 MB.",
        )

    try:
        from PIL import Image, UnidentifiedImageError
        import io

        try:
            img = Image.open(io.BytesIO(raw))
            img.verify()                       # confirms it's a genuine image
            img = Image.open(io.BytesIO(raw))  # re-open: verify() consumes the parser
        except UnidentifiedImageError:
            raise HTTPException(
                status_code=415,
                detail="That file doesn't look like a valid image. Please try a different photo.",
            )

        if min(img.size) < MIN_PHOTO_DIMENSION:
            raise HTTPException(
                status_code=422,
                detail=f"Image is too small — please use a photo at least "
                       f"{MIN_PHOTO_DIMENSION}x{MIN_PHOTO_DIMENSION}px.",
            )

        # Normalise to RGB (flattens PNG/WEBP transparency onto white so it
        # never renders as a broken/black square once saved as JPEG) and
        # downscale large photos to keep storage and load times sane.
        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            background.paste(rgba, mask=rgba.split()[-1])
            img = background
        else:
            img = img.convert("RGB")

        img.thumbnail((MAX_PHOTO_DIMENSION, MAX_PHOTO_DIMENSION), Image.LANCZOS)

        old_path = current_user.get("photo_path")

        token = secrets.token_hex(6)
        filename = f"user_{current_user['id']}_{token}.jpg"
        dest = PHOTO_DIR / filename
        img.save(dest, format="JPEG", quality=87, optimize=True)

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Photo processing failed for user {current_user['id']}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not process that image. Please try again.")

    photo_url = f"/uploads/profile_photos/{filename}"
    try:
        db.execute(
            "UPDATE users SET photo_path = ?, profile_updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (photo_url, current_user["id"]),
        )
    except Exception as exc:
        logger.error(f"Saving photo path failed for user {current_user['id']}: {exc}")
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Could not save the photo. Please try again.")

    # Best-effort cleanup of the previous photo file, now that the new one
    # is confirmed saved and the DB points at it.
    if old_path:
        try:
            old_name = old_path.rsplit("/", 1)[-1]
            old_file = PHOTO_DIR / old_name
            if old_file.exists() and old_file.resolve().parent == PHOTO_DIR.resolve():
                old_file.unlink()
        except Exception as exc:
            logger.warning(f"Could not remove previous photo for user {current_user['id']}: {exc}")

    logger.info(f"Profile photo updated: user {current_user['id']}")
    return _profile_payload(_get_user_row(current_user["id"]))


@router.delete("/photo")
async def delete_profile_photo(current_user: dict = Depends(get_current_user)):
    """Remove the current photo, reverting the avatar to initials."""
    old_path = current_user.get("photo_path")
    db.execute(
        "UPDATE users SET photo_path = NULL, profile_updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (current_user["id"],),
    )
    if old_path:
        try:
            old_name = old_path.rsplit("/", 1)[-1]
            old_file = PHOTO_DIR / old_name
            if old_file.exists() and old_file.resolve().parent == PHOTO_DIR.resolve():
                old_file.unlink()
        except Exception as exc:
            logger.warning(f"Could not remove photo file for user {current_user['id']}: {exc}")
    return _profile_payload(_get_user_row(current_user["id"]))
