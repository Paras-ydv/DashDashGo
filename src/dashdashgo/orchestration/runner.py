"""Entry point for starting runs - synchronously (CLI) or in the background (API, scheduler).

Whatever the trigger, a run goes through the same steps: load and validate the
config (fail fast, before any browser starts), take the per-report lock, create
the run record, then hand over to the one orchestrator.
"""

from __future__ import annotations

import fcntl
import logging
import os
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from dashdashgo.config.loader import ReportRegistry
from dashdashgo.config.models import ReportConfig
from dashdashgo.errors import ConcurrentRunError, ConfigurationError
from dashdashgo.metadata.models import RunRecord, RunStatus, Trigger, new_run_id, utcnow
from dashdashgo.metadata.repository import RunRepository
from dashdashgo.observability.logging import log_context
from dashdashgo.orchestration.pipeline import PipelineOrchestrator, RunResult

log = logging.getLogger(__name__)


class ReportLock:
    """Cross-process, per-report lock (flock on a file in the shared storage volume).

    Prevents a scheduled run and a manual "Run now" of the same report from
    downloading and loading concurrently. The OS releases the lock if the
    process dies, so a crash can never leave a report permanently locked.
    """

    def __init__(self, lock_dir: Path) -> None:
        self._dir = lock_dir

    @contextmanager
    def hold(self, report: str) -> Iterator[None]:
        self._dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._dir / f"{report}.lock", os.O_CREAT | os.O_RDWR, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ConcurrentRunError(f"a run of '{report}' is already in progress") from None
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class RunService:
    def __init__(
        self,
        *,
        registry: ReportRegistry,
        orchestrator: PipelineOrchestrator,
        runs: RunRepository,
        lock: ReportLock,
        max_workers: int = 2,
    ) -> None:
        self._registry = registry
        self._orchestrator = orchestrator
        self._runs = runs
        self._lock = lock
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="run")
        self._active: set[str] = set()

    def _prepare(
        self, name: str, trigger: Trigger, parent_run_id: str | None
    ) -> tuple[ReportConfig, RunRecord]:
        report = self._registry.load(name)  # raises ConfigurationError before anything starts
        if not report.enabled and trigger is Trigger.SCHEDULE:
            raise ConfigurationError(f"report '{name}' is disabled")
        run = RunRecord(
            run_id=new_run_id(),
            report=name,
            trigger=trigger,
            status=RunStatus.QUEUED,
            parent_run_id=parent_run_id,
            started_at=utcnow(),
            destination_table=report.destination.qualified_table,
        )
        return report, run

    def _execute(self, report: ReportConfig, run: RunRecord, force: bool) -> RunResult:
        with log_context(run_id=run.run_id, report=report.name):
            try:
                with self._lock.hold(report.name):
                    return self._orchestrator.run(report, run, force=force)
            except ConcurrentRunError as exc:
                failed = run.model_copy(
                    update={
                        "status": RunStatus.FAILED,
                        "finished_at": utcnow(),
                        "duration_ms": 0,
                        "error_type": exc.error_type,
                        "error_message": exc.message,
                        "error_stage": "queued",
                    }
                )
                self._runs.save_run(failed)
                log.warning("Not started: %s", exc.message)
                return RunResult(failed)
            finally:
                self._active.discard(report.name)

    def run_now(
        self,
        name: str,
        *,
        trigger: Trigger = Trigger.CLI,
        force: bool = False,
        parent_run_id: str | None = None,
    ) -> RunResult:
        """Run in the calling thread and return the finished run."""
        report, run = self._prepare(name, trigger, parent_run_id)
        self._runs.save_run(run)
        return self._execute(report, run, force)

    def submit(
        self,
        name: str,
        *,
        trigger: Trigger = Trigger.API,
        force: bool = False,
        parent_run_id: str | None = None,
        on_done: Callable[[RunResult], None] | None = None,
    ) -> RunRecord:
        """Queue a run in the background and return its (QUEUED) record immediately."""
        report, run = self._prepare(name, trigger, parent_run_id)
        if name in self._active:
            raise ConcurrentRunError(f"a run of '{name}' is already queued or running")
        self._active.add(name)
        self._runs.save_run(run)
        future: Future[RunResult] = self._executor.submit(self._execute, report, run, force)
        if on_done:
            future.add_done_callback(lambda f: on_done(f.result()))
        log.info("Queued run %s of '%s' (trigger=%s)", run.run_id, name, trigger.value)
        return run

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=True)
