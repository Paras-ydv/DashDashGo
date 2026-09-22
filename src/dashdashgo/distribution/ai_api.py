"""AI assistant endpoints (all refuse with 503 while AI_API_KEY is unset).

GET  /api/ai                               is the assistant on, which models
POST /api/runs/{run_id}/diagnose           diagnose a failed run (stored with the run)
GET  /api/runs/{run_id}/diagnosis          the stored diagnosis, if any
POST /api/reports/{name}/config/draft      draft a config from a sample export file
"""

from __future__ import annotations

import base64
import binascii
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, StringConstraints

from dashdashgo.ai.assistant import MAX_SAMPLE_BYTES, AIAssistant
from dashdashgo.config.loader import REPORT_NAME
from dashdashgo.distribution.api import _run_or_404
from dashdashgo.distribution.state import AppState, get_state
from dashdashgo.errors import AIError, AINotConfiguredError, ConfigurationError, DashDashGoError
from dashdashgo.metadata.models import RunStatus

router = APIRouter(prefix="/api", tags=["ai"])


def _assistant(state: AppState) -> AIAssistant:
    assistant = state.container.assistant
    if assistant is None or not assistant.enabled:
        raise HTTPException(503, "the AI assistant is off: set AI_API_KEY and restart")
    return assistant


def _ai_error(exc: DashDashGoError) -> HTTPException:
    if isinstance(exc, AINotConfiguredError):
        return HTTPException(503, exc.message)
    if isinstance(exc, AIError):
        return HTTPException(502, exc.message)
    return HTTPException(503, exc.message)


@router.get("/ai")
def ai_status(request: Request) -> dict[str, Any]:
    state = get_state(request)
    settings = state.container.settings
    return {
        "enabled": state.ai_enabled,
        "base_url": settings.ai_base_url,
        "models": [m.strip() for m in settings.ai_model.split(",") if m.strip()],
    }


@router.post("/runs/{run_id}/diagnose")
def diagnose(request: Request, run_id: str) -> dict[str, Any]:
    state = get_state(request)
    assistant = _assistant(state)
    run = _run_or_404(state, run_id)
    if run.status is not RunStatus.FAILED:
        raise HTTPException(409, f"only failed runs can be diagnosed (this one is {run.status})")
    try:
        return assistant.diagnose(run_id).model_dump(mode="json")
    except DashDashGoError as exc:
        raise _ai_error(exc) from exc


@router.get("/runs/{run_id}/diagnosis")
def stored_diagnosis(request: Request, run_id: str) -> dict[str, Any]:
    state = get_state(request)
    run = _run_or_404(state, run_id)
    found = state.container.assistant.stored_diagnosis(run) if state.container.assistant else None
    if found is None:
        raise HTTPException(404, f"run {run_id} has no AI diagnosis")
    return found.model_dump(mode="json")


class DraftRequest(BaseModel):
    filename: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    content_base64: str = Field(max_length=MAX_SAMPLE_BYTES * 4 // 3 + 8)
    from_report: str | None = None


@router.post("/reports/{name}/config/draft")
def draft_config(request: Request, name: str, body: DraftRequest) -> dict[str, Any]:
    state = get_state(request)
    assistant = _assistant(state)
    if not REPORT_NAME.fullmatch(name):
        raise HTTPException(422, "name must be lower_snake_case (2-63 chars, starting a-z)")
    try:
        data = base64.b64decode(body.content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(422, "content_base64 is not valid base64") from exc
    try:
        draft = assistant.draft(name, body.filename, data, from_report=body.from_report)
    except ConfigurationError as exc:  # e.g. an unknown "from_report"
        raise HTTPException(422, exc.message) from exc
    except DashDashGoError as exc:
        if isinstance(exc, AIError) and not isinstance(exc, AINotConfiguredError):
            # Sample problems (format, empty file) are the caller's to fix.
            status = 422 if "sample" in exc.message or "cannot read" in exc.message else 502
            raise HTTPException(status, exc.message) from exc
        raise _ai_error(exc) from exc
    return {
        "name": draft.name,
        "yaml": draft.yaml,
        "notes": draft.notes,
        "problems": [{"location": loc, "message": msg} for loc, msg in draft.problems],
        "model": draft.model,
    }
