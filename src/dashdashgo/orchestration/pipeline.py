"""The pipeline orchestrator: one run of one report, start to finish.

    preflight -> acquisition -> parse -> transform -> quality -> dedup -> load -> verify

The orchestrator only sequences services and records what happened; the work
itself lives in AcquisitionService, IngestionService and WarehouseLoader.
Manual (CLI), API, retry and scheduled runs all come through ``run()``.
"""

from __future__ import annotations

import io
import json
import logging
import socket
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pyarrow.lib import ArrowException

from dashdashgo.acquisition.service import AcquisitionService
from dashdashgo.config.models import ReportConfig
from dashdashgo.errors import DashDashGoError, StorageError
from dashdashgo.ingestion.service import IngestionService, PreparedDataset
from dashdashgo.metadata.models import RunRecord, RunStatus, utcnow
from dashdashgo.metadata.repository import RunRepository
from dashdashgo.metadata.tracker import RunTracker
from dashdashgo.observability.logging import log_context, redactor, run_log_file
from dashdashgo.orchestration.context import RunContext
from dashdashgo.storage import StorageBackend
from dashdashgo.utils.retry import RetryPolicy, call_with_retry
from dashdashgo.warehouse.loader import WarehouseLoader

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunResult:
    run: RunRecord

    @property
    def ok(self) -> bool:
        return self.run.status in (RunStatus.SUCCESS, RunStatus.SKIPPED)


class PipelineOrchestrator:
    def __init__(
        self,
        *,
        acquisition: AcquisitionService,
        ingestion: IngestionService,
        loader: WarehouseLoader,
        runs: RunRepository,
        storage: StorageBackend,
    ) -> None:
        self._acquisition = acquisition
        self._ingestion = ingestion
        self._loader = loader
        self._runs = runs
        self._storage = storage

    def run(
        self,
        report: ReportConfig,
        run: RunRecord,
        *,
        force: bool = False,
        overrides: Sequence[str] = (),
    ) -> RunResult:
        redactor.register(report.source.credentials.password.get_secret_value())
        tracker = RunTracker(self._runs, run)
        try:
            with (
                tempfile.TemporaryDirectory(prefix=f"ddg-{run.run_id}-") as tmp,
                log_context(run_id=run.run_id, report=report.name),
            ):
                ctx = RunContext(run=run, workdir=Path(tmp), force=force)
                with (
                    self._storage.writer(ctx.artifacts.key("logs", "run.log")) as log_path,
                    run_log_file(log_path, run.run_id),
                ):
                    if overrides:
                        log.info("Config overrides for this run: %s", ", ".join(overrides))
                    self._execute(report, ctx, tracker)
                self._write_summary(ctx, tracker.run)
        except BaseException as exc:
            # Setup failures (log file, temp dir) and interrupts (Ctrl-C) happen outside
            # the stage machinery: still end the run instead of leaving it RUNNING/QUEUED.
            if not tracker.run.status.is_terminal:
                tracker.fail(
                    exc if isinstance(exc, Exception) else InterruptedError("run was interrupted")
                )
            if not isinstance(exc, Exception):
                raise
            log.exception("Run %s could not be executed", run.run_id)
        return RunResult(tracker.run)

    def _execute(self, report: ReportConfig, ctx: RunContext, tracker: RunTracker) -> None:
        tracker.start()
        tracker.update(executed_on=f"{socket.gethostname()}:{self._storage.location}")
        log.info(
            "Starting pipeline for '%s' (trigger=%s%s)",
            report.name,
            tracker.run.trigger.value,
            ", force" if ctx.force else "",
        )
        policy = RetryPolicy.from_config(report.retry)
        destination = report.destination
        tracker.update(destination_table=destination.qualified_table)
        try:
            with tracker.stage("config") as stage:
                stage.message = f"{report.source.platform} -> {destination.qualified_table}"

            with tracker.stage("preflight") as stage:
                outcome = call_with_retry(
                    lambda _: self._loader.prepare(destination),
                    policy,
                    description="Warehouse preflight",
                )
                stage.message = f"table {destination.qualified_table} {outcome}"

            acquired = self._acquisition.acquire(report, ctx, tracker)
            tracker.update(source_file=acquired.storage_key, file_hash=acquired.sha256)

            with tracker.stage("parse") as stage:
                raw = self._ingestion.parse(
                    acquired.local_path, report.source.export.format, report.ingestion
                )
                stage.message = f"{len(raw):,} rows, {len(raw.columns)} columns"
            tracker.update(records_downloaded=len(raw))

            with tracker.stage("transform") as stage:
                transformed = self._ingestion.transform(raw, report.ingestion)
                stage.message = (
                    f"{len(transformed):,} rows after {len(report.ingestion.transforms)} step(s)"
                )

            with tracker.stage("quality") as stage:
                dataset = self._ingestion.validate(transformed, report)
                quality = dataset.quality
                stage.message = f"{quality.valid_rows:,} valid, {quality.rejected_rows:,} rejected"
                stage.details = dataset.quality.as_dict()
                self._store_rejected(ctx, dataset, stage.details)
            tracker.update(
                records_rejected=dataset.quality.rejected_rows, data_hash=dataset.fingerprint
            )
            self._store_processed(ctx, dataset)

            with tracker.stage("dedup") as stage:
                previous = self._runs.find_ingested(report.name, dataset.fingerprint)
                duplicate = previous is not None and previous.run_id != ctx.run_id
                stage.message = (
                    f"identical data already loaded by run {previous.run_id}"
                    if duplicate and previous
                    else "new data"
                )
                if duplicate and ctx.force:
                    stage.message += " (forced reload)"
            if duplicate and not ctx.force and previous:
                for skipped in ("load", "verify"):
                    tracker.skipped_stage(skipped, f"skipped: duplicate of run {previous.run_id}")
                tracker.skip(duplicate_of=previous.run_id)
                log.info("SKIPPED - data identical to run %s; nothing loaded", previous.run_id)
                return

            ingested_at = utcnow()
            with tracker.stage("load") as stage:
                inserted = call_with_retry(
                    lambda _: self._loader.load(
                        destination, dataset.frame, ctx.run_id, ingested_at
                    ),
                    policy,
                    description="ClickHouse insert",
                )
                stage.message = f"{inserted:,} rows -> {destination.qualified_table}"

            with tracker.stage("verify") as stage:
                found = self._loader.verify(destination, ctx.run_id, inserted)
                stage.message = f"{found:,} rows confirmed in ClickHouse"
            tracker.succeed(records_inserted=found)
            log.info("SUCCESS - %d rows loaded into %s", found, destination.qualified_table)
        except DashDashGoError as exc:
            tracker.fail(exc)
            log.error("FAILED at %s - %s: %s", tracker.run.error_stage, exc.error_type, exc.message)
        except Exception as exc:
            # A bug, not an anticipated failure: keep the traceback, mark the run
            # failed (never leave it RUNNING) and let the caller see the status.
            tracker.fail(exc)
            log.exception("FAILED with an unexpected error")

    def _store_rejected(
        self, ctx: RunContext, dataset: PreparedDataset, details: dict[str, object]
    ) -> None:
        if dataset.rejected.empty:
            return
        buffer = io.StringIO()
        dataset.rejected.to_csv(buffer, index=False)
        stored = self._storage.put_bytes(
            buffer.getvalue().encode(), ctx.artifacts.key("failures", "rejected_rows.csv")
        )
        details["rejected_file"] = stored.key

    def _store_processed(self, ctx: RunContext, dataset: PreparedDataset) -> None:
        """Typed Parquet copy of the loaded rows - useful, but not worth failing a run for."""
        local = ctx.workdir / "processed.parquet"
        try:
            dataset.frame.to_parquet(local, index=False)
            self._storage.put_file(local, ctx.artifacts.key("processed", "data.parquet"))
        except (StorageError, OSError, ValueError, TypeError, ArrowException) as exc:
            log.warning("Could not store the processed Parquet copy: %s", exc)

    def _write_summary(self, ctx: RunContext, run: RunRecord) -> None:
        """A self-contained record of the run next to its log, readable without ClickHouse."""
        try:
            self._storage.put_bytes(
                json.dumps(run.model_dump(mode="json"), indent=2).encode(),
                ctx.artifacts.key("logs", "run.json"),
            )
        except StorageError as exc:
            log.error("Could not write run summary: %s", exc.message)
