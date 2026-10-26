"""
EasyRecruit ATS 3.0 - Main Application Entry Point
"""
import logging
import time
from contextlib import asynccontextmanager


from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# NOTE: BaseHTTPMiddleware lives in starlette (which FastAPI bundles).
# fastapi.middleware.base does NOT exist — always use starlette.middleware.base
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.routes import router
from app.api.auth_routes import router as auth_router
from app.api.job_routes import router as job_router
from app.api.analysis_routes import router as analysis_router
from app.api.recommendation_routes import router as recommendation_router
from app.api.notification_routes import router as notification_router
from app.api.interview_routes import router as interview_router
from app.api.fraud_routes import router as fraud_router
from app.api.bias_routes import router as bias_router
from app.api.profile_routes import router as profile_router, PHOTO_DIR
from app.db.database import init_db
from app.config import settings, describe_cors, describe_scoring_policy, describe_upload_limits
from app.json_safety import SafeJSONResponse
from app.request_limits import RequestSizeLimitMiddleware

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── Middleware classes ────────────────────────────────────────────────────────
class RequestTimingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = (time.perf_counter() - start) * 1000
        response.headers["X-Process-Time-Ms"] = f"{elapsed:.2f}"
        if elapsed > 3000:
            logger.warning(
                f"Slow request [{response.status_code}]: "
                f"{request.method} {request.url.path} took {elapsed:.0f}ms"
            )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 56)
    logger.info("  EasyRecruit ATS 3.0  —  Starting up")
    logger.info(f"  Version : {settings.API_VERSION}")
    logger.info(f"  URL     : http://{settings.HOST}:{settings.PORT}")
    logger.info(f"  Debug   : {settings.DEBUG}")
    logger.info(f"  CORS    : {describe_cors(settings.CORS_ORIGINS)}")
    logger.info(f"  Uploads : {describe_upload_limits(settings)}")
    logger.info(f"  Scoring : {describe_scoring_policy(settings)}")
    logger.info("=" * 56)
    try:
        init_db()
        logger.info("Database ready.")
    except Exception as exc:
        logger.critical(f"Database init failed — cannot start safely: {exc}", exc_info=True)
        raise  # Re-raise so the process exits rather than serving with a broken DB
    yield
    logger.info("EasyRecruit ATS 3.0 — Shutdown complete.")


# ── App factory ───────────────────────────────────────────────────────────────
def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.API_TITLE,
        version=settings.API_VERSION,
        description=settings.API_DESCRIPTION,
        docs_url="/api/docs"         if settings.DEBUG else None,
        redoc_url="/api/redoc"       if settings.DEBUG else None,
        openapi_url="/api/openapi.json" if settings.DEBUG else None,
        lifespan=lifespan,
        # NaN / Infinity can never reach a client as invalid JSON or an unhandled 500 (app/json_safety.py, Task 7).
        default_response_class=SafeJSONResponse,
    )

    # Middleware — order: outermost added last with add_middleware
    # Request-size guard is added FIRST, so it is the innermost layer: it rejects an oversized body before
    # any route parses it, while its 413 still passes through the security-header, timing and CORS layers.
    app.add_middleware(RequestSizeLimitMiddleware)
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestTimingMiddleware)
    app.add_middleware(
        CORSMiddleware,
        # Validated allowlist from CORS_ORIGINS (app/config.py). Never a hardcoded "*", and credentials
        # are only ever enabled for an explicit allowlist.
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Process-Time-Ms"],
    )

    # Routers
    app.include_router(router)
    app.include_router(auth_router)
    app.include_router(job_router)
    app.include_router(analysis_router)
    app.include_router(recommendation_router)
    app.include_router(notification_router)
    app.include_router(interview_router)
    app.include_router(fraud_router)
    app.include_router(bias_router)
    app.include_router(profile_router)

    # Frontend pages
    @app.get("/", include_in_schema=False)
    async def root():
        return FileResponse("frontend/index.html")

    @app.get("/dashboard", include_in_schema=False)
    @app.get("/dashboard.html", include_in_schema=False)
    async def dashboard():
        return FileResponse("frontend/dashboard.html")

    @app.get("/index.html", include_in_schema=False)
    async def index_html():
        return FileResponse("frontend/index.html")

    # Global exception handler — catches anything not already an HTTPException
    @app.exception_handler(Exception)
    async def global_exc_handler(request: Request, exc: Exception):
        logger.error(
            f"Unhandled exception: {request.method} {request.url.path} → {type(exc).__name__}: {exc}",
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "An unexpected error occurred. Please try again or contact support."},
        )

    @app.exception_handler(404)
    async def not_found_handler(request: Request, exc: Exception):
        # Only return JSON for API routes; let the frontend handle page 404s
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                status_code=404,
                content={"detail": f"Endpoint '{request.url.path}' not found."},
            )
        return JSONResponse(status_code=404, content={"detail": "Not found."})

    # Static files -- separate try/except per mount. These are independent
    # concerns (the pre-existing frontend mount uses a relative path and is
    # sensitive to the working directory the app was launched from; the
    # profile-photos mount uses an absolute path and doesn't share that
    # fragility) -- bundling them meant a failure in the first silently
    # prevented the second from ever being attempted, which is exactly how
    # every uploaded profile photo ended up 404ing even though the rest of
    # the app loaded and worked normally.
    try:
        app.mount("/static", StaticFiles(directory="frontend"), name="static")
    except Exception as exc:
        logger.warning(f"Frontend static files not mounted: {exc}")
    try:
        app.mount("/uploads/profile_photos", StaticFiles(directory=str(PHOTO_DIR)), name="profile_photos")
    except Exception as exc:
        logger.warning(f"Profile photo static files not mounted: {exc}")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level=settings.LOG_LEVEL.lower(),
        access_log=True,
    )
