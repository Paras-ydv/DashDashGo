"""Pipeline run metadata: one record per run, one per stage (attempt)."""

from __future__ import annotations

import secrets
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, Field


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    SKIPPED = "SKIPPED"  # identical data already ingested - nothing to do
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in (RunStatus.SUCCESS, RunStatus.SKIPPED, RunStatus.FAILED)


class StageStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class Trigger(StrEnum):
    CLI = "cli"  # `dashdashgo run` in a terminal / container
    API = "api"  # "Run now" in the UI or POST /api/reports/{name}/runs
    SCHEDULE = "schedule"
    RETRY = "retry"  # re-run of a failed run from the UI/API

    @property
    def executes_in_server(self) -> bool:
        return self is not Trigger.CLI


def utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    """ClickHouse returns naive datetimes for UTC columns; make them explicit."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(_as_utc)]


def new_run_id(now: datetime | None = None) -> str:
    """Sortable, readable and unique enough: ``20260921-081503-7f3a9c``."""
    now = now or utcnow()
    return f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


class RunRecord(BaseModel):
    run_id: str
    report: str
    trigger: Trigger
    status: RunStatus
    parent_run_id: str | None = None
    started_at: UtcDatetime
    finished_at: UtcDatetime | None = None
    duration_ms: int | None = None
    current_stage: str = ""
    attempts: int = 0
    records_downloaded: int = 0
    records_rejected: int = 0
    records_inserted: int = 0
    destination_table: str = ""
    source_file: str = ""
    file_hash: str = ""
    data_hash: str = ""
    duplicate_of: str | None = None
    error_type: str = ""
    error_message: str = ""
    error_stage: str = ""
    updated_at: UtcDatetime = Field(default_factory=utcnow)

    @property
    def run_date(self) -> date:
        return self.started_at.astimezone(UTC).date()

    @property
    def duration_seconds(self) -> float | None:
        return self.duration_ms / 1000 if self.duration_ms is not None else None


class StageRecord(BaseModel):
    run_id: str
    report: str
    stage: str
    attempt: int = 1
    status: StageStatus
    started_at: UtcDatetime
    finished_at: UtcDatetime | None = None
    duration_ms: int | None = None
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
    updated_at: UtcDatetime = Field(default_factory=utcnow)


class ReportStats(BaseModel):
    report: str
    total_runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    skipped_runs: int = 0
    rows_inserted: int = 0
    avg_duration_ms: float | None = None
    last_run: RunRecord | None = None
    recent_runs: list[RunRecord] = Field(default_factory=list)
    """Most recent runs, oldest first."""


class OverviewStats(BaseModel):
    window_days: int
    total_runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    skipped_runs: int = 0
    running_runs: int = 0
    rows_inserted: int = 0
    rows_rejected: int = 0
    median_duration_ms: float | None = None

    @property
    def success_rate(self) -> float | None:
        finished = self.successful_runs + self.failed_runs + self.skipped_runs
        return (self.successful_runs + self.skipped_runs) / finished if finished else None
