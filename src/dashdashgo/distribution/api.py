"""JSON API: pipeline status for operators, report data for downstream consumers.

GET  /api/health                         liveness + dependency status
GET  /api/stats?days=7                   aggregate run statistics
GET  /api/reports                        configured reports + latest status
GET  /api/reports/{name}                 masked config, schedule, stats, DDL
POST /api/reports/{name}/runs            start a run (202 + run record)
GET  /api/reports/{name}/data            ingested rows (JSON or ?format=csv)
GET  /api/runs                           run history (?report=&status=)
GET  /api/runs/{run_id}                  run + timeline + artifacts
GET  /api/runs/{run_id}/logs             structured log lines of the run
POST /api/runs/{run_id}/retry            re-run a finished run's report
GET  /api/artifacts/{key}                download a stored artifact
"""

from __future__ import annotations

import csv
import io
import mimetypes
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from dashdashgo import __version__
from dashdashgo.distribution.state import AppState, get_state
from dashdashgo.distribution.views import (
    build_timeline,
    list_artifacts,
    masked_config,
    read_run_log,
)
from dashdashgo.errors import (
    ConcurrentRunError,
    ConfigurationError,
    DashDashGoError,
    ReportNotConfiguredError,
    StorageError,
)
from dashdashgo.metadata.models import RunRecord, RunStatus, Trigger
from dashdashgo.storage import AREAS
from dashdashgo.warehouse.ddl import create_table_sql

router = APIRouter(prefix="/api", tags=["api"])

Limit = Annotated[int, Query(ge=1, le=10_000)]
Offset = Annotated[int, Query(ge=0)]


def _jsonable(value: Any) -> Any:
    """Decimals become strings so monetary values never lose precision in transit."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def _error(exc: DashDashGoError) -> HTTPException:
    if isinstance(exc, ReportNotConfiguredError):
        return HTTPException(404, exc.message)
    if isinstance(exc, ConcurrentRunError):
        return HTTPException(409, exc.message)
    if isinstance(exc, ConfigurationError):
        return HTTPException(422, exc.message)
    return HTTPException(503, exc.message)


def _run_or_404(state: AppState, run_id: str) -> RunRecord:
    try:
        run = state.container.runs.get_run(run_id)
    except DashDashGoError as exc:
        raise _error(exc) from exc
    if run is None:
        raise HTTPException(404, f"run {run_id} not found")
    return run


@router.get("/health")
def health(request: Request) -> JSONResponse:
    state = get_state(request)
    clickhouse = state.container.clickhouse.ping()
    body = {
        "status": "ok" if clickhouse else "degraded",
        "version": __version__,
        "clickhouse": "up" if clickhouse else "down",
        "scheduler": "running" if state.scheduler and state.scheduler.running else "stopped",
        "reports": len(state.container.registry.names()),
    }
    return JSONResponse(body, status_code=200 if clickhouse else 503)


@router.get("/stats")
def stats(request: Request, days: Annotated[int, Query(ge=1, le=365)] = 7) -> dict[str, Any]:
    state = get_state(request)
    try:
        overview = state.container.runs.overview(days)
    except DashDashGoError as exc:
        raise _error(exc) from exc
    return {**overview.model_dump(), "success_rate": overview.success_rate}


@router.get("/reports")
def list_reports(request: Request) -> list[dict[str, Any]]:
    state = get_state(request)
    valid, invalid = state.container.registry.load_all()
    result: list[dict[str, Any]] = []
    for name, config in valid.items():
        try:
            report_stats = state.container.runs.report_stats(name).model_dump(mode="json")
        except DashDashGoError:
            report_stats = None
        result.append(
            {
                "name": name,
                "description": config.description,
                "enabled": config.enabled,
                "format": config.source.export.format.value,
                "destination": config.destination.qualified_table,
                "schedule": {
                    **config.schedule.model_dump(),
                    "next_run": state.next_run(name),
                },
                "stats": report_stats,
                "valid": True,
            }
        )
    result += [{"name": n, "valid": False, "error": e.message} for n, e in invalid.items()]
    return result


@router.get("/reports/{name}")
def get_report(request: Request, name: str) -> dict[str, Any]:
    state = get_state(request)
    try:
        config = state.container.registry.load(name)
        report_stats = state.container.runs.report_stats(name)
    except DashDashGoError as exc:
        raise _error(exc) from exc
    return {
        "name": name,
        "config": masked_config(config),
        "schedule": {**config.schedule.model_dump(), "next_run": state.next_run(name)},
        "stats": report_stats.model_dump(mode="json"),
        "ddl": create_table_sql(config.destination),
    }


@router.post("/reports/{name}/runs", status_code=202)
def start_run(request: Request, name: str, force: bool = False) -> dict[str, Any]:
    state = get_state(request)
    try:
        run = state.container.run_service.submit(name, trigger=Trigger.API, force=force)
    except DashDashGoError as exc:
        raise _error(exc) from exc
    return run.model_dump(mode="json")


@router.get("/reports/{name}/data", response_model=None)
def report_data(
    request: Request,
    name: str,
    limit: Limit = 100,
    offset: Offset = 0,
    since: date | None = None,
    until: date | None = None,
    run_id: str | None = None,
    format: Annotated[str, Query(pattern="^(json|csv)$")] = "json",
) -> dict[str, Any] | StreamingResponse:
    state = get_state(request)
    try:
        config = state.container.registry.load(name)
        page = state.container.data_reader.fetch(
            config.destination, limit=limit, offset=offset, since=since, until=until, run_id=run_id
        )
    except DashDashGoError as exc:
        raise _error(exc) from exc
    if format == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=page.columns)
        writer.writeheader()
        writer.writerows({k: _jsonable(v) for k, v in row.items()} for row in page.rows)
        return StreamingResponse(
            iter([buffer.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
        )
    return {
        "report": name,
        "table": config.destination.qualified_table,
        "columns": page.columns,
        "total": page.total,
        "limit": page.limit,
        "offset": page.offset,
        "rows": [{k: _jsonable(v) for k, v in row.items()} for row in page.rows],
    }


@router.get("/runs")
def list_runs(
    request: Request,
    report: str | None = None,
    status: RunStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Offset = 0,
) -> list[dict[str, Any]]:
    state = get_state(request)
    try:
        runs = state.container.runs.list_runs(
            report=report, status=status, limit=limit, offset=offset
        )
    except DashDashGoError as exc:
        raise _error(exc) from exc
    return [r.model_dump(mode="json") for r in runs]


@router.get("/runs/{run_id}")
def get_run(request: Request, run_id: str) -> dict[str, Any]:
    state = get_state(request)
    run = _run_or_404(state, run_id)
    stages = state.container.runs.stages(run_id)
    artifacts = list_artifacts(state.container.storage, run)
    return {
        "run": run.model_dump(mode="json"),
        "timeline": [asdict(step) for step in build_timeline(run, stages)],
        "artifacts": {area: [asdict(a) for a in items] for area, items in artifacts.items()},
    }


@router.get("/runs/{run_id}/logs")
def run_logs(request: Request, run_id: str) -> list[dict[str, str]]:
    state = get_state(request)
    run = _run_or_404(state, run_id)
    return [asdict(line) for line in read_run_log(state.container.storage, run)]


@router.post("/runs/{run_id}/retry", status_code=202)
def retry_run(request: Request, run_id: str) -> dict[str, Any]:
    state = get_state(request)
    previous = _run_or_404(state, run_id)
    if not previous.status.is_terminal:
        raise HTTPException(409, f"run {run_id} is still {previous.status.value.lower()}")
    try:
        run = state.container.run_service.submit(
            previous.report, trigger=Trigger.RETRY, parent_run_id=run_id
        )
    except DashDashGoError as exc:
        raise _error(exc) from exc
    return run.model_dump(mode="json")


@router.get("/artifacts/{key:path}")
def artifact(request: Request, key: str) -> Response:
    state = get_state(request)
    if key.split("/", 1)[0] not in AREAS:
        raise HTTPException(404, "not an artifact")
    try:
        data = state.container.storage.read_bytes(key)
    except StorageError as exc:
        raise HTTPException(404, exc.message) from exc
    media_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
    if media_type == "text/html":
        media_type = "text/plain"  # never render captured pages in our origin
    name = key.rsplit("/", 1)[-1]
    disposition = "inline" if media_type.startswith(("image/", "text/")) else "attachment"
    return Response(
        data,
        media_type=media_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
