"""FastAPI application: JSON API + operations UI + in-process scheduler."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from dashdashgo import __version__
from dashdashgo.container import Container, build_container, seed_bundled_reports
from dashdashgo.distribution import ai_api, api, config_api, ui
from dashdashgo.distribution.security import install_security
from dashdashgo.distribution.state import AppState
from dashdashgo.errors import DashDashGoError
from dashdashgo.scheduling.scheduler import ReportScheduler
from dashdashgo.settings import Settings
from dashdashgo.utils.retry import RetryPolicy, call_with_retry

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def _initialise_metadata(container: Container) -> None:
    """Create metadata tables, waiting for ClickHouse if it is still starting."""
    try:
        call_with_retry(
            lambda _: container.runs.ensure_schema(),
            RetryPolicy(max_attempts=8, initial_delay=1, multiplier=2, max_delay=15),
            description="Metadata schema setup",
        )
        interrupted = container.runs.mark_interrupted(
            "the server restarted before this run completed"
        )
        if interrupted:
            log.warning(
                "Marked %d run(s) interrupted by the previous shutdown as FAILED", interrupted
            )
    except DashDashGoError as exc:
        log.error("ClickHouse not ready; the API starts degraded: %s", exc.message)


def create_app(
    settings: Settings, *, container: Container | None = None, scheduler: bool | None = None
) -> FastAPI:
    container = container or build_container(settings)
    run_scheduler = settings.scheduler_enabled if scheduler is None else scheduler

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        seed_bundled_reports(settings)
        _initialise_metadata(container)
        report_scheduler = None
        if run_scheduler:
            report_scheduler = ReportScheduler(
                registry=container.registry,
                run_service=container.run_service,
                storage=container.storage,
                retention_days=settings.storage_retention_days,
            )
            report_scheduler.start()
        app.state.ddg = AppState(container=container, scheduler=report_scheduler)
        try:
            yield
        finally:
            if report_scheduler:
                report_scheduler.shutdown()
            container.run_service.shutdown(wait=False)

    app = FastAPI(
        title="DashDashGo",
        version=__version__,
        description="Dashboard report acquisition, ingestion and distribution.",
        lifespan=lifespan,
    )
    install_security(app, settings)
    app.include_router(api.router)
    app.include_router(config_api.router)
    app.include_router(ai_api.router)
    app.include_router(ui.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(StarletteHTTPException)
    async def not_found(request: Request, exc: StarletteHTTPException) -> Response:
        """API clients get JSON; people browsing the UI get a page, not raw JSON."""
        if exc.status_code == 404 and not request.url.path.startswith(("/api/", "/static/")):
            return ui.not_found_page(request, str(exc.detail))
        return await http_exception_handler(request, exc)

    return app
