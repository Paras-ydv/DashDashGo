"""In-memory test doubles for infrastructure-facing interfaces."""

from __future__ import annotations

import threading
from datetime import timedelta
from statistics import median

from dashdashgo.metadata.models import (
    OverviewStats,
    ReportStats,
    RunRecord,
    RunStatus,
    StageRecord,
    utcnow,
)
from dashdashgo.metadata.repository import RunRepository


class InMemoryRunRepository(RunRepository):
    def __init__(self) -> None:
        self.runs: dict[str, RunRecord] = {}
        self.stage_events: dict[tuple[str, str, int], StageRecord] = {}
        self.run_history: list[RunRecord] = []
        self._lock = threading.Lock()

    def ensure_schema(self) -> None:
        pass

    def save_run(self, run: RunRecord) -> None:
        with self._lock:
            self.runs[run.run_id] = run
            self.run_history.append(run)

    def save_stage(self, stage: StageRecord) -> None:
        with self._lock:
            self.stage_events[(stage.run_id, stage.stage, stage.attempt)] = stage

    def get_run(self, run_id: str) -> RunRecord | None:
        return self.runs.get(run_id)

    def list_runs(
        self,
        *,
        report: str | None = None,
        status: RunStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RunRecord]:
        runs = sorted(self.runs.values(), key=lambda r: r.started_at, reverse=True)
        runs = [
            r
            for r in runs
            if (not report or r.report == report) and (not status or r.status == status)
        ]
        return runs[offset : offset + limit]

    def stages(self, run_id: str) -> list[StageRecord]:
        return sorted(
            (s for s in self.stage_events.values() if s.run_id == run_id),
            key=lambda s: (s.started_at, s.attempt),
        )

    def latest_load(self, report: str) -> RunRecord | None:
        loads = [
            r for r in self.runs.values() if r.report == report and r.status == RunStatus.SUCCESS
        ]
        return max(loads, key=lambda r: r.started_at) if loads else None

    def overview(self, window_days: int) -> OverviewStats:
        since = utcnow() - timedelta(days=window_days)
        runs = [r for r in self.runs.values() if r.started_at >= since]
        durations = [
            r.duration_ms
            for r in runs
            if r.duration_ms is not None and r.status != RunStatus.FAILED
        ]
        return OverviewStats(
            window_days=window_days,
            total_runs=len(runs),
            successful_runs=sum(r.status == RunStatus.SUCCESS for r in runs),
            failed_runs=sum(r.status == RunStatus.FAILED for r in runs),
            skipped_runs=sum(r.status == RunStatus.SKIPPED for r in runs),
            running_runs=sum(not r.status.is_terminal for r in runs),
            rows_inserted=sum(r.records_inserted for r in runs),
            rows_rejected=sum(r.records_rejected for r in runs),
            median_duration_ms=median(durations) if durations else None,
        )

    def report_stats(self, report: str, recent: int = 20) -> ReportStats:
        runs = self.list_runs(report=report, limit=10_000)
        return ReportStats(
            report=report,
            total_runs=len(runs),
            successful_runs=sum(r.status == RunStatus.SUCCESS for r in runs),
            failed_runs=sum(r.status == RunStatus.FAILED for r in runs),
            skipped_runs=sum(r.status == RunStatus.SKIPPED for r in runs),
            rows_inserted=sum(r.records_inserted for r in runs),
            last_run=runs[0] if runs else None,
            recent_runs=list(reversed(runs[:recent])),
        )

    def mark_interrupted(self, message: str) -> int:
        count = 0
        from dashdashgo.metadata.repository import STALE_AFTER

        stale = utcnow() - STALE_AFTER
        for run in list(self.runs.values()):
            if not run.status.is_terminal and (
                run.trigger.executes_in_server or run.updated_at < stale
            ):
                self.save_run(
                    run.model_copy(update={"status": RunStatus.FAILED, "error_message": message})
                )
                count += 1
        return count
