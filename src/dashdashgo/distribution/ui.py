"""Server-rendered operations UI.

Every number on every page comes from the run metadata in ClickHouse or from
stored artifacts; pages that show an unfinished run refresh themselves until
it completes. Actions (run, retry) call the JSON API from the browser.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from dashdashgo import __version__
from dashdashgo.config.models import ReportConfig
from dashdashgo.distribution.state import AppState, get_state
from dashdashgo.distribution.views import (
    build_timeline,
    list_artifacts,
    masked_config_yaml,
    read_run_log,
    status_state,
)
from dashdashgo.errors import DashDashGoError
from dashdashgo.metadata.models import ReportStats, RunRecord, RunStatus
from dashdashgo.warehouse.ddl import create_table_sql

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


# --- formatting helpers exposed to templates --------------------------------------


def fmt_duration(ms: float | None) -> str:
    if ms is None:
        return "—"
    seconds = ms / 1000
    if seconds < 1:
        return f"{int(ms)} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs:02d}s"


def fmt_count(value: float | None) -> str:
    if value is None:
        return "—"
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e4, "K")):
        if abs(value) >= limit:
            return f"{value / limit:.1f}{suffix}"
    return f"{int(value):,}"


def fmt_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    power = min(int(math.log(size, 1024)) if size > 0 else 0, len(units) - 1)
    return f"{size / 1024**power:.1f} {units[power]}" if power else f"{size} B"


def fmt_ago(moment: datetime | None) -> str:
    if moment is None:
        return "never"
    delta = (datetime.now(UTC) - moment).total_seconds()
    future = delta < 0
    delta = abs(delta)
    for unit, seconds in (("d", 86400), ("h", 3600), ("m", 60)):
        if delta >= seconds:
            amount = f"{int(delta // seconds)}{unit}"
            return f"in {amount}" if future else f"{amount} ago"
    return "in <1m" if future else "just now"


def fmt_time(moment: datetime | None) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC") if moment else "—"


templates.env.filters.update(
    duration=fmt_duration, count=fmt_count, bytes=fmt_bytes, ago=fmt_ago, utc=fmt_time
)
templates.env.globals.update(status_state=status_state, version=__version__)


def _render(request: Request, template: str, **context: Any) -> HTMLResponse:
    state = get_state(request)
    context.setdefault("nav_reports", state.container.registry.names())
    return templates.TemplateResponse(request, template, context)


def _unavailable(request: Request, exc: DashDashGoError) -> HTMLResponse:
    response = _render(request, "unavailable.html", error=exc.message)
    response.status_code = 503
    return response


def _report_summary(
    state: AppState, name: str, config: ReportConfig, stats: ReportStats
) -> dict[str, Any]:
    return {"name": name, "config": config, "stats": stats, "next_run": state.next_run(name)}


# --- pages --------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def overview(request: Request) -> HTMLResponse:
    state = get_state(request)
    valid, invalid = state.container.registry.load_all()
    try:
        stats = state.container.runs.overview(7)
        reports = [
            _report_summary(state, name, cfg, state.container.runs.report_stats(name))
            for name, cfg in valid.items()
        ]
        recent = state.container.runs.list_runs(limit=12)
    except DashDashGoError as exc:
        return _unavailable(request, exc)
    live = any(not r.status.is_terminal for r in recent)
    return _render(
        request,
        "overview.html",
        stats=stats,
        reports=reports,
        invalid=invalid,
        recent=recent,
        live=live,
        scheduled=sum(1 for r in reports if r["config"].schedule.enabled and r["config"].enabled),
        page="overview",
    )


@router.get("/reports/{name}", response_class=HTMLResponse)
def report_page(request: Request, name: str) -> HTMLResponse:
    state = get_state(request)
    try:
        config = state.container.registry.load(name)
    except DashDashGoError as exc:
        raise HTTPException(404, exc.message) from exc
    try:
        stats = state.container.runs.report_stats(name, recent=30)
        runs = state.container.runs.list_runs(report=name, limit=30)
    except DashDashGoError as exc:
        return _unavailable(request, exc)
    preview = None
    preview_error = ""
    try:
        preview = state.container.data_reader.fetch(config.destination, limit=15)
    except DashDashGoError as exc:
        preview_error = exc.message
    return _render(
        request,
        "report.html",
        name=name,
        config=config,
        stats=stats,
        runs=runs,
        chart_runs=list(reversed([r for r in runs if r.duration_ms is not None][:30])),
        next_run=state.next_run(name),
        config_yaml=masked_config_yaml(config),
        ddl=create_table_sql(config.destination),
        preview=preview,
        preview_error=preview_error,
        live=any(not r.status.is_terminal for r in runs[:5]),
        page=name,
    )


@router.get("/runs", response_class=HTMLResponse)
def runs_page(
    request: Request, report: str | None = None, status: str | None = None, page_no: int = 1
) -> HTMLResponse:
    state = get_state(request)
    per_page = 50
    status_filter = RunStatus(status) if status and status in RunStatus.__members__ else None
    try:
        runs = state.container.runs.list_runs(
            report=report or None,
            status=status_filter,
            limit=per_page + 1,
            offset=(max(page_no, 1) - 1) * per_page,
        )
    except DashDashGoError as exc:
        return _unavailable(request, exc)
    return _render(
        request,
        "runs.html",
        runs=runs[:per_page],
        has_next=len(runs) > per_page,
        page_no=max(page_no, 1),
        report=report or "",
        status=status_filter.value if status_filter else "",
        statuses=[s.value for s in RunStatus],
        live=any(not r.status.is_terminal for r in runs[:per_page]),
        page="runs",
    )


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: str) -> HTMLResponse:
    state = get_state(request)
    try:
        run: RunRecord | None = state.container.runs.get_run(run_id)
        if run is None:
            raise HTTPException(404, f"run {run_id} not found")
        stages = state.container.runs.stages(run_id)
    except DashDashGoError as exc:
        return _unavailable(request, exc)
    artifacts = list_artifacts(state.container.storage, run)
    screenshots = artifacts.get("screenshots", [])
    return _render(
        request,
        "run.html",
        run=run,
        timeline=build_timeline(run, stages),
        artifacts=artifacts,
        screenshots=screenshots,
        logs=read_run_log(state.container.storage, run),
        live=not run.status.is_terminal,
        page="runs",
    )
