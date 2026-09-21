"""FastAPI application: JSON API + operations UI + in-process scheduler."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from dashdashgo import __version__
from dashdashgo.container import Container, build_container
from dashdashgo.distribution import api, ui
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
    app.include_router(api.router)
    app.include_router(ui.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
