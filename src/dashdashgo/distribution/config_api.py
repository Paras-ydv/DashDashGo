"""Config management API - backs the UI's pipeline editor.

    GET    /api/config-templates?name=&source=           starting YAML for a new report
    POST   /api/reports                                  create a report  {name, yaml}
    DELETE /api/reports/{name}                           archive a report (file is kept)
    GET    /api/reports/{name}/config                    raw YAML + version
    POST   /api/reports/{name}/config/validate           dry-run validation {yaml}
    PUT    /api/reports/{name}/config                    save {yaml, base_version}
    GET    /api/reports/{name}/config/history            previous versions
    GET    /api/reports/{name}/config/history/{version}  one previous version

Raw YAML is served as written, so secrets appear only as ``${ENV_VAR}``
references; the store refuses to save a plaintext secret.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from dashdashgo.config.models import ReportConfig
from dashdashgo.distribution.state import AppState, get_state
from dashdashgo.errors import (
    ConfigConflictError,
    ConfigurationError,
    DashDashGoError,
    ReportNotConfiguredError,
    WarehouseError,
)
from dashdashgo.warehouse.ddl import create_table_sql

router = APIRouter(prefix="/api", tags=["config"])


class ConfigText(BaseModel):
    yaml: str = Field(max_length=200_000)


class SaveConfig(ConfigText):
    base_version: str


class CreateReport(ConfigText):
    name: str


def _problems(exc: ConfigurationError) -> list[dict[str, str]]:
    return [{"location": loc, "message": msg} for loc, msg in exc.problems] or [
        {"location": "(root)", "message": exc.message}
    ]


def _raise(exc: DashDashGoError) -> HTTPException:
    if isinstance(exc, ReportNotConfiguredError):
        return HTTPException(404, exc.message)
    if isinstance(exc, ConfigConflictError):
        return HTTPException(409, exc.message)
    if isinstance(exc, ConfigurationError):
        return HTTPException(422, {"message": exc.message, "problems": _problems(exc)})
    return HTTPException(503, exc.message)


def _summary(state: AppState, config: ReportConfig) -> dict[str, Any]:
    destination = config.destination
    warnings: list[str] = []
    valid, _ = state.container.registry.load_all()
    if sharing := [
        other
        for other, cfg in valid.items()
        if other != config.name and cfg.destination.qualified_table == destination.qualified_table
    ]:
        warnings.append(
            f"{destination.qualified_table} is also written by {', '.join(sharing)}; "
            "rows with the same key will overwrite each other"
        )
    try:
        drift = state.container.loader.schema_drift(destination)
    except WarehouseError as exc:
        drift = None
        warnings.append(f"Could not compare with the existing table: {exc.message}")
    if drift:
        warnings.append(
            f"{destination.qualified_table} already exists with a different schema "
            f"({'; '.join(drift)}). Runs will fail preflight until the table is migrated."
        )
    return {
        "format": config.source.export.format.value,
        "location": " / ".join(
            [*config.source.location.collection, config.source.location.target_name]
            + ([config.source.location.card] if config.source.location.card else [])
        ),
        "destination": destination.qualified_table,
        "table_exists": drift is not None,
        "columns": len(destination.columns),
        "transforms": [step.describe() for step in config.ingestion.transforms],
        "rules": len(config.ingestion.quality.rules),
        "schedule": (
            f"{config.schedule.cron} ({config.schedule.timezone})"
            if config.schedule.enabled and config.schedule.cron
            else "on demand"
        ),
        "enabled": config.enabled,
        "ddl": create_table_sql(destination),
        "warnings": warnings,
    }


def _resync(state: AppState) -> None:
    if state.scheduler:
        state.scheduler.sync()


@router.get("/config-templates")
def config_template(
    request: Request, name: str = "new_report", source: str | None = None
) -> dict[str, str]:
    state = get_state(request)
    try:
        return {"yaml": state.container.config_store.template(name, source)}
    except DashDashGoError as exc:
        raise _raise(exc) from exc


@router.post("/reports", status_code=201)
def create_report(request: Request, body: CreateReport) -> dict[str, Any]:
    state = get_state(request)
    try:
        document = state.container.config_store.create(body.name, body.yaml)
    except DashDashGoError as exc:
        raise _raise(exc) from exc
    _resync(state)
    return {"name": document.name, "version": document.version}


@router.delete("/reports/{name}")
def archive_report(request: Request, name: str) -> dict[str, str]:
    state = get_state(request)
    try:
        archived = state.container.config_store.archive(name)
    except DashDashGoError as exc:
        raise _raise(exc) from exc
    _resync(state)
    return {"name": name, "archived_as": archived}


@router.get("/reports/{name}/config")
def get_config(request: Request, name: str) -> dict[str, Any]:
    state = get_state(request)
    try:
        document = state.container.config_store.read(name)
    except DashDashGoError as exc:
        raise _raise(exc) from exc
    return {
        "name": document.name,
        "yaml": document.text,
        "version": document.version,
        "modified_at": document.modified_at.isoformat(),
    }


@router.post("/reports/{name}/config/validate")
def validate_config(request: Request, name: str, body: ConfigText) -> dict[str, Any]:
    """Dry run: validate without saving. Always 200; ``valid`` says the outcome."""
    state = get_state(request)
    try:
        config = state.container.config_store.check(name, body.yaml)
    except ConfigurationError as exc:
        return {"valid": False, "message": exc.message.splitlines()[0], "problems": _problems(exc)}
    return {"valid": True, "problems": [], "summary": _summary(state, config)}


@router.put("/reports/{name}/config")
def save_config(request: Request, name: str, body: SaveConfig) -> dict[str, Any]:
    state = get_state(request)
    try:
        document = state.container.config_store.save(name, body.yaml, body.base_version)
    except DashDashGoError as exc:
        raise _raise(exc) from exc
    _resync(state)
    return {
        "name": name,
        "version": document.version,
        "modified_at": document.modified_at.isoformat(),
    }


@router.get("/reports/{name}/config/history")
def config_history(request: Request, name: str) -> list[dict[str, Any]]:
    state = get_state(request)
    try:
        versions = state.container.config_store.history(name)
    except DashDashGoError as exc:
        raise _raise(exc) from exc
    return [{"id": v.id, "saved_at": v.saved_at.isoformat(), "size": v.size} for v in versions]


@router.get("/reports/{name}/config/history/{version_id}")
def config_version(request: Request, name: str, version_id: str) -> dict[str, str]:
    state = get_state(request)
    try:
        return {
            "id": version_id,
            "yaml": state.container.config_store.read_version(name, version_id),
        }
    except DashDashGoError as exc:
        raise _raise(exc) from exc
