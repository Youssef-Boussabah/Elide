"""FastAPI application entry point: `uv run uvicorn api.app.main:app --reload`."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from api.app.demo_ai import router as demo_ai_router
from api.config import get_settings
from api.db import check_database

VERSION = "0.0.1"


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Elide API", version=VERSION)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        """Liveness: the process is up. Does not touch the database or PayPal."""
        return {"status": "ok", "service": "elide-api", "version": VERSION}

    @app.get("/health/ready", response_model=None)
    def ready() -> JSONResponse:
        """Readiness: database reachable. Reports configuration flags, never secret values."""
        flags = {
            "paypal_sandbox_configured": settings.paypal_configured,
            "llm_configured": bool(settings.llm_provider and settings.llm_api_key),
        }
        try:
            version = check_database()
        except SQLAlchemyError as exc:
            body = {
                "status": "unavailable",
                "database": "unreachable",
                "error": type(exc).__name__,
                **flags,
            }
            return JSONResponse(status_code=503, content=body)
        return JSONResponse(
            content={"status": "ok", "database": "ok", "postgres_version": version, **flags}
        )

    app.include_router(demo_ai_router)
    return app


app = create_app()
