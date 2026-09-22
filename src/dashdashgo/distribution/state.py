"""Application state held by the FastAPI app (the container + scheduler)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fastapi import Request

from dashdashgo.container import Container
from dashdashgo.scheduling.scheduler import ReportScheduler


@dataclass
class AppState:
    container: Container
    scheduler: ReportScheduler | None

    @property
    def ai_enabled(self) -> bool:
        assistant = self.container.assistant
        return assistant is not None and assistant.enabled

    def next_run(self, report: str) -> datetime | None:
        return self.scheduler.next_run(report) if self.scheduler else None


def get_state(request: Request) -> AppState:
    state: AppState = request.app.state.ddg
    return state
