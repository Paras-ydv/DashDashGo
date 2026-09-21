"""Records a run's lifecycle and its stages as they happen.

The UI's live timeline is simply this data: each stage is written once when it
starts (RUNNING) and again when it ends (SUCCESS/FAILED + duration + message).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from dashdashgo.errors import DashDashGoError, WarehouseError
from dashdashgo.metadata.models import RunRecord, RunStatus, StageRecord, StageStatus, utcnow
from dashdashgo.metadata.repository import RunRepository
from dashdashgo.observability.logging import log_context

log = logging.getLogger(__name__)


class StageHandle:
    """Lets the code inside a stage attach a summary message and details."""

    def __init__(self, record: StageRecord) -> None:
        self.record = record
        self.message = ""
        self.details: dict[str, Any] = {}


class RunTracker:
    def __init__(self, repository: RunRepository, run: RunRecord) -> None:
        self._repo = repository
        self.run = run

    # --- persistence ----------------------------------------------------------

    def _persist(self, action: str, fn: Any, *args: Any) -> None:
        """Metadata writes must never mask the pipeline's own outcome.

        If ClickHouse is unreachable, the failure is logged loudly (and the run
        log file still has the full story), but the pipeline error - or success
        - is what propagates.
        """
        try:
            fn(*args)
        except WarehouseError as exc:
            log.error("Could not record %s in run metadata: %s", action, exc.message)

    def _save_run(self) -> None:
        self._persist("run state", self._repo.save_run, self.run)

    def update(self, **fields: Any) -> None:
        self.run = self.run.model_copy(update=fields)
        self._save_run()

    # --- lifecycle ------------------------------------------------------------

    def queued(self) -> None:
        self.update(status=RunStatus.QUEUED)

    def start(self) -> None:
        self.update(status=RunStatus.RUNNING, started_at=utcnow())

    def _finish(self, status: RunStatus, **fields: Any) -> None:
        finished = utcnow()
        duration = int((finished - self.run.started_at).total_seconds() * 1000)
        self.update(status=status, finished_at=finished, duration_ms=duration, **fields)

    def succeed(self, **fields: Any) -> None:
        self._finish(RunStatus.SUCCESS, **fields)

    def skip(self, **fields: Any) -> None:
        self._finish(RunStatus.SKIPPED, **fields)

    def fail(self, exc: BaseException) -> None:
        if isinstance(exc, DashDashGoError):
            error_type, message, stage = exc.error_type, exc.message, exc.stage
        else:
            error_type, message, stage = type(exc).__name__, str(exc), "pipeline"
        self._finish(
            RunStatus.FAILED,
            error_type=error_type,
            error_message=message[:4000],
            error_stage=self.run.current_stage or stage,
        )

    # --- stages ---------------------------------------------------------------

    @contextmanager
    def stage(self, name: str, attempt: int = 1) -> Iterator[StageHandle]:
        record = StageRecord(
            run_id=self.run.run_id,
            report=self.run.report,
            stage=name,
            attempt=attempt,
            status=StageStatus.RUNNING,
            started_at=utcnow(),
        )
        handle = StageHandle(record)
        if "." not in name:  # sub-steps (acquisition.login) don't move the headline stage
            self.update(current_stage=name)
        self._persist("stage start", self._repo.save_stage, record)
        started = time.perf_counter()
        with log_context(stage=name):
            try:
                yield handle
            except BaseException as exc:
                elapsed = int((time.perf_counter() - started) * 1000)
                message = exc.message if isinstance(exc, DashDashGoError) else str(exc)
                artifacts = exc.artifacts if isinstance(exc, DashDashGoError) else []
                self._persist(
                    "stage failure",
                    self._repo.save_stage,
                    record.model_copy(
                        update={
                            "status": StageStatus.FAILED,
                            "finished_at": utcnow(),
                            "duration_ms": elapsed,
                            "message": f"{type(exc).__name__}: {message}"[:2000],
                            "details": {**handle.details, "artifacts": artifacts},
                        }
                    ),
                )
                log.error("✗ %s failed after %.1fs: %s", name, elapsed / 1000, message)
                raise
            elapsed = int((time.perf_counter() - started) * 1000)
            self._persist(
                "stage completion",
                self._repo.save_stage,
                record.model_copy(
                    update={
                        "status": StageStatus.SUCCESS,
                        "finished_at": utcnow(),
                        "duration_ms": elapsed,
                        "message": handle.message,
                        "details": handle.details,
                    }
                ),
            )
            log.info(
                "✓ %s (%.1fs)%s",
                name,
                elapsed / 1000,
                f" {handle.message}" if handle.message else "",
            )

    def skipped_stage(self, name: str, message: str) -> None:
        now = utcnow()
        self._persist(
            "skipped stage",
            self._repo.save_stage,
            StageRecord(
                run_id=self.run.run_id,
                report=self.run.report,
                stage=name,
                status=StageStatus.SKIPPED,
                started_at=now,
                finished_at=now,
                duration_ms=0,
                message=message,
            ),
        )
