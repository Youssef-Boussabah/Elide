"""FastAPI application entry point: `uv run uvicorn api.app.main:app --reload`."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from api.app.auth.routes import router as auth_router
from api.app.demo_ai import router as demo_ai_router
from api.app.webhooks import router as webhook_router
from api.app.workspace import router as workspace_router
from api.app.workspace import workflow_error_handler
from api.config import get_settings
from api.db import check_database
from api.execution.runtime import build_provider, build_verifier
from api.workflow.store import WorkflowError

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
    # Test seams: tests may replace these; production uses the real engine and clock.
    app.state.engine = None
    app.state.clock = None
    app.state.session_policy = None
    app.state.workspace_llm = None  # no paid inference unless explicitly configured
    # P5 (ADR-0012): both default to "off". The verifier exists only with PAYPAL_WEBHOOK_ID;
    # the provider only with PAYMENT_PROVIDER (none by default). Provider-mutating calls are
    # made by the worker, never by a request handler.
    app.state.webhook_verifier = build_verifier(settings)
    app.state.payment_provider = build_provider(settings)
    app.add_exception_handler(WorkflowError, workflow_error_handler)

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
    app.include_router(auth_router)
    app.include_router(workspace_router)
    app.include_router(webhook_router)
    return app


app = create_app()
