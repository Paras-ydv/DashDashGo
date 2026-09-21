"""Orchestration: the full pipeline with fake acquisition/warehouse, plus acquisition
retry/screenshot behaviour driven through a real headless browser and a fake adapter."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from playwright.sync_api import Page

from dashdashgo.acquisition.adapters.base import DashboardAdapter
from dashdashgo.acquisition.service import AcquiredReport, AcquisitionService
from dashdashgo.config.models import DestinationConfig, ReportConfig
from dashdashgo.errors import AuthenticationError, LoadError, NavigationError
from dashdashgo.ingestion.service import IngestionService
from dashdashgo.metadata.models import (
    RunRecord,
    RunStatus,
    StageStatus,
    Trigger,
    new_run_id,
    utcnow,
)
from dashdashgo.metadata.tracker import RunTracker
from dashdashgo.orchestration.context import RunContext
from dashdashgo.orchestration.pipeline import PipelineOrchestrator
from dashdashgo.storage import LocalStorage
from tests.fakes import InMemoryRunRepository

CSV = "Date,Region,Revenue\n2026-09-14,East,10.50\n2026-09-14,West,-3\n2026-09-15,East,7\n"


class FakeAcquisition(AcquisitionService):
    """Returns a prepared file instead of driving a browser."""

    def __init__(self, storage: LocalStorage, content: str, error: Exception | None = None) -> None:
        super().__init__(storage)
        self.content = content
        self.error = error

    def acquire(self, report: ReportConfig, ctx: RunContext, tracker: RunTracker) -> AcquiredReport:
        with tracker.stage("acquisition"):
            if self.error:
                raise self.error
            path = ctx.workdir / "report.csv"
            path.write_text(self.content)
            key = ctx.artifacts.key("raw", "report.csv")
            self._storage.put_file(path, key)
            return AcquiredReport(path, key, len(self.content), "f" * 64, 1)


class FakeLoader:
    def __init__(self, fail_times: int = 0) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.fail_times = fail_times
        self.load_calls = 0

    def prepare(self, destination: DestinationConfig) -> str:
        return "verified"

    def load(
        self,
        destination: DestinationConfig,
        frame: pd.DataFrame,
        run_id: str,
        ingested_at: datetime,
    ) -> int:
        self.load_calls += 1
        if self.load_calls <= self.fail_times:
            raise LoadError("too many parts")
        rows = [dict(r, _run_id=run_id) for r in frame.to_dict(orient="records")]
        self.tables.setdefault(destination.qualified_table, []).extend(rows)
        return len(rows)

    def verify(self, destination: DestinationConfig, run_id: str, expected_rows: int) -> int:
        return sum(1 for r in self.tables[destination.qualified_table] if r["_run_id"] == run_id)


@pytest.fixture
def report(
    config_dict: dict[str, Any], make_config: Callable[[dict[str, Any]], ReportConfig]
) -> ReportConfig:
    config_dict["ingestion"]["quality"]["max_invalid_ratio"] = 0.5
    return make_config(config_dict)


def new_run(name: str) -> RunRecord:
    return RunRecord(
        run_id=new_run_id(),
        report=name,
        trigger=Trigger.CLI,
        status=RunStatus.QUEUED,
        started_at=utcnow(),
    )


def build(
    tmp_path: Path, acquisition_error: Exception | None = None, loader: FakeLoader | None = None
):  # type: ignore[no-untyped-def]
    storage = LocalStorage(tmp_path / "storage")
    runs = InMemoryRunRepository()
    loader = loader or FakeLoader()
    orchestrator = PipelineOrchestrator(
        acquisition=FakeAcquisition(storage, CSV, acquisition_error),
        ingestion=IngestionService(),
        loader=loader,  # type: ignore[arg-type]
        runs=runs,
        storage=storage,
    )
    return orchestrator, runs, loader, storage


def test_successful_run_records_everything(tmp_path: Path, report: ReportConfig) -> None:
    orchestrator, runs, _, storage = build(tmp_path)
    result = orchestrator.run(report, new_run(report.name))

    run = result.run
    assert result.ok and run.status is RunStatus.SUCCESS
    assert (run.records_downloaded, run.records_rejected, run.records_inserted) == (3, 1, 2)
    assert run.data_hash and run.finished_at and run.duration_ms is not None
    stages = {s.stage: s.status for s in runs.stages(run.run_id)}
    assert all(
        stages[s] is StageStatus.SUCCESS
        for s in ("config", "preflight", "parse", "transform", "quality", "dedup", "load", "verify")
    )

    prefix = f"{report.name}/{run.run_date}/{run.run_id}"
    assert storage.exists(f"raw/{prefix}/report.csv")
    assert storage.exists(f"processed/{prefix}/data.parquet")
    assert storage.exists(f"failures/{prefix}/rejected_rows.csv")  # the negative revenue row
    assert storage.exists(f"logs/{prefix}/run.log") and storage.exists(f"logs/{prefix}/run.json")
    assert b"below minimum" in storage.read_bytes(f"failures/{prefix}/rejected_rows.csv")


def test_identical_data_is_skipped_and_force_reloads(tmp_path: Path, report: ReportConfig) -> None:
    orchestrator, runs, loader, _ = build(tmp_path)
    first = orchestrator.run(report, new_run(report.name)).run
    second = orchestrator.run(report, new_run(report.name)).run
    assert second.status is RunStatus.SKIPPED
    assert second.duplicate_of == first.run_id
    assert loader.load_calls == 1
    assert {s.stage: s.status for s in runs.stages(second.run_id)}["load"] is StageStatus.SKIPPED

    forced = orchestrator.run(report, new_run(report.name), force=True).run
    assert forced.status is RunStatus.SUCCESS and loader.load_calls == 2


def test_acquisition_failure_marks_run_failed_with_stage(
    tmp_path: Path, report: ReportConfig
) -> None:
    orchestrator, _, loader, _ = build(
        tmp_path, acquisition_error=AuthenticationError("bad password")
    )
    run = orchestrator.run(report, new_run(report.name)).run
    assert run.status is RunStatus.FAILED
    assert (run.error_type, run.error_stage) == ("AuthenticationError", "acquisition")
    assert loader.load_calls == 0


def test_transient_load_failure_is_retried(tmp_path: Path, report: ReportConfig) -> None:
    orchestrator, _, loader, _ = build(tmp_path, loader=FakeLoader(fail_times=1))
    run = orchestrator.run(report, new_run(report.name)).run
    assert run.status is RunStatus.SUCCESS
    assert loader.load_calls == 2


def test_unexpected_exception_never_leaves_run_running(
    tmp_path: Path, report: ReportConfig
) -> None:
    orchestrator, _, _, _ = build(tmp_path, acquisition_error=ZeroDivisionError("bug"))
    run = orchestrator.run(report, new_run(report.name)).run
    assert run.status is RunStatus.FAILED
    assert run.error_type == "ZeroDivisionError"


def test_password_never_reaches_run_log(tmp_path: Path, report: ReportConfig) -> None:
    orchestrator, _, _, storage = build(
        tmp_path, acquisition_error=AuthenticationError("s3cret-Passw0rd rejected")
    )
    run = orchestrator.run(report, new_run(report.name)).run
    log = storage.read_bytes(f"logs/{report.name}/{run.run_date}/{run.run_id}/run.log")
    assert b"s3cret-Passw0rd" not in log
    assert b"********" in log


# --- acquisition service with a real browser ------------------------------------------


class ScriptedAdapter(DashboardAdapter):
    """Renders local HTML; fails according to a script to exercise retries."""

    platform = "scripted"

    def __init__(self, failures: list[Exception]) -> None:
        self.failures = failures
        self.logins = 0

    def prepare(self, page: Page) -> None:
        page.set_content("<h1>Scripted dashboard</h1>")

    def login(self, page: Page) -> None:
        self.logins += 1

    def open_report(self, page: Page) -> None:
        if self.failures:
            raise self.failures.pop(0)

    def apply_filters(self, page: Page) -> str:
        return "none"

    def download(self, page: Page, target_dir: Path) -> Path:
        path = target_dir / "report.csv"
        path.write_text(CSV)
        return path


class RejectingAdapter(ScriptedAdapter):
    def login(self, page: Page) -> None:
        raise AuthenticationError("rejected")


chromium_missing = (
    shutil.which("chromium") is None
    and not any(Path.home().glob("Library/Caches/ms-playwright/chromium*"))
    and not any(Path("/ms-playwright").glob("chromium*"))
)


@pytest.mark.skipif(chromium_missing, reason="Playwright Chromium not installed")
def test_acquisition_retries_and_screenshots_each_failure(
    tmp_path: Path, report: ReportConfig
) -> None:
    storage = LocalStorage(tmp_path / "storage")
    adapter = ScriptedAdapter([NavigationError("dashboard did not render")])
    service = AcquisitionService(storage, adapter_factory=lambda _: adapter)
    runs = InMemoryRunRepository()
    run = new_run(report.name)
    ctx = RunContext(run=run, workdir=tmp_path / "work")
    ctx.workdir.mkdir()

    acquired = service.acquire(report, ctx, RunTracker(runs, run))

    assert acquired.attempts == 2 and adapter.logins == 2
    shots = storage.list(f"screenshots/{report.name}/")
    assert [Path(s.key).name for s in shots] == ["attempt1_navigate_failure.png"]
    attempts = sorted(
        (s.attempt, s.status) for s in runs.stages(run.run_id) if s.stage == "acquisition"
    )
    assert attempts == [(1, StageStatus.FAILED), (2, StageStatus.SUCCESS)]


@pytest.mark.skipif(chromium_missing, reason="Playwright Chromium not installed")
def test_acquisition_does_not_retry_authentication_errors(
    tmp_path: Path, report: ReportConfig
) -> None:
    storage = LocalStorage(tmp_path / "storage")
    adapter = RejectingAdapter([])
    service = AcquisitionService(storage, adapter_factory=lambda _: adapter)
    run = new_run(report.name)
    ctx = RunContext(run=run, workdir=tmp_path / "work")
    ctx.workdir.mkdir()
    with pytest.raises(AuthenticationError) as info:
        service.acquire(report, ctx, RunTracker(InMemoryRunRepository(), run))
    assert any("attempt1_login_failure.png" in a for a in info.value.artifacts)
    assert len(storage.list(f"screenshots/{report.name}/")) == 1
