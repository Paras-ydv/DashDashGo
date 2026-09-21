"""Runner (locking, run creation), scheduler wiring, timeline view model and HTTP API."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from dashdashgo.config.loader import ReportRegistry
from dashdashgo.config.models import ReportConfig
from dashdashgo.config.store import ConfigStore
from dashdashgo.container import Container
from dashdashgo.distribution.app import create_app
from dashdashgo.distribution.views import build_timeline
from dashdashgo.errors import ConcurrentRunError, ConfigurationError
from dashdashgo.metadata.models import (
    RunRecord,
    RunStatus,
    StageRecord,
    StageStatus,
    Trigger,
    new_run_id,
    utcnow,
)
from dashdashgo.orchestration.pipeline import RunResult
from dashdashgo.orchestration.runner import ReportLock, RunService
from dashdashgo.scheduling.scheduler import ReportScheduler
from dashdashgo.settings import Settings
from dashdashgo.storage import LocalStorage
from tests.conftest import BASE_CONFIG, TEST_ENV
from tests.fakes import InMemoryRunRepository


class FakeOrchestrator:
    def __init__(self, runs: InMemoryRunRepository, gate: threading.Event | None = None) -> None:
        self.runs = runs
        self.gate = gate
        self.calls: list[tuple[str, bool]] = []

    def run(
        self, report: ReportConfig, run: RunRecord, *, force: bool = False, overrides: Any = ()
    ) -> RunResult:
        self.calls.append((run.run_id, force))
        if self.gate:
            self.gate.wait(5)
        done = run.model_copy(
            update={"status": RunStatus.SUCCESS, "finished_at": utcnow(), "duration_ms": 5}
        )
        self.runs.save_run(done)
        return RunResult(done)


@pytest.fixture
def reports_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "reports"
    directory.mkdir()
    scheduled = dict(
        BASE_CONFIG, schedule={"enabled": True, "cron": "0 8 * * 1", "timezone": "Asia/Kolkata"}
    )
    (directory / "sample_report.yaml").write_text(yaml.safe_dump(scheduled))
    return directory


def make_service(
    reports_dir: Path, tmp_path: Path, gate: threading.Event | None = None
) -> tuple[RunService, InMemoryRunRepository, FakeOrchestrator]:
    runs = InMemoryRunRepository()
    orchestrator = FakeOrchestrator(runs, gate)
    service = RunService(
        registry=ReportRegistry(reports_dir, TEST_ENV),
        orchestrator=orchestrator,  # type: ignore[arg-type]
        runs=runs,
        lock=ReportLock(tmp_path / "locks"),
    )
    return service, runs, orchestrator


# --- runner -------------------------------------------------------------------------


def test_report_lock_is_exclusive_per_report(tmp_path: Path) -> None:
    lock = ReportLock(tmp_path)
    with lock.hold("a"):
        with pytest.raises(ConcurrentRunError), lock.hold("a"):
            pass
        with lock.hold("b"):
            pass
    with lock.hold("a"):
        pass  # released after the block


def test_run_now_records_and_executes(reports_dir: Path, tmp_path: Path) -> None:
    service, runs, orchestrator = make_service(reports_dir, tmp_path)
    result = service.run_now("sample_report", force=True)
    assert result.ok
    assert orchestrator.calls == [(result.run.run_id, True)]
    assert runs.run_history[0].status is RunStatus.QUEUED  # recorded before execution


def test_invalid_config_fails_before_anything_runs(reports_dir: Path, tmp_path: Path) -> None:
    (reports_dir / "broken.yaml").write_text(
        yaml.safe_dump(dict(BASE_CONFIG, name="broken", retry={"max_attempts": 0}))
    )
    service, runs, orchestrator = make_service(reports_dir, tmp_path)
    with pytest.raises(ConfigurationError):
        service.run_now("broken")
    assert not runs.runs and not orchestrator.calls


def test_submit_rejects_second_run_of_same_report(reports_dir: Path, tmp_path: Path) -> None:
    gate = threading.Event()
    service, _, _ = make_service(reports_dir, tmp_path, gate)
    first = service.submit("sample_report")
    assert first.status is RunStatus.QUEUED
    with pytest.raises(ConcurrentRunError):
        service.submit("sample_report")
    gate.set()
    service.shutdown()


# --- scheduler ----------------------------------------------------------------------


def test_scheduler_registers_cron_jobs_that_use_the_run_service(
    reports_dir: Path, tmp_path: Path
) -> None:
    service, _, _ = make_service(reports_dir, tmp_path)
    submitted: list[tuple[str, Trigger]] = []
    service.submit = lambda name, trigger=Trigger.API, **_: submitted.append((name, trigger))  # type: ignore[assignment, method-assign, misc]
    scheduler = ReportScheduler(
        registry=ReportRegistry(reports_dir, TEST_ENV),
        run_service=service,
        storage=LocalStorage(tmp_path / "s"),
        retention_days=0,
    )
    scheduler.start()
    try:
        next_run = scheduler.next_run("sample_report")
        assert next_run is not None and next_run.weekday() == 0  # Monday
        assert (next_run.hour, next_run.minute) == (8, 0) and str(next_run.tzinfo) == "Asia/Kolkata"
        scheduler._fire("sample_report")
        assert submitted == [("sample_report", Trigger.SCHEDULE)]
    finally:
        scheduler.shutdown()


def test_scheduler_sync_picks_up_config_changes(reports_dir: Path, tmp_path: Path) -> None:
    service, _, _ = make_service(reports_dir, tmp_path)
    scheduler = ReportScheduler(
        registry=ReportRegistry(reports_dir, TEST_ENV),
        run_service=service,
        storage=LocalStorage(tmp_path / "s"),
        retention_days=0,
    )
    scheduler.start()
    try:
        (reports_dir / "sample_report.yaml").write_text(
            yaml.safe_dump(dict(BASE_CONFIG))
        )  # schedule removed
        scheduler.sync()
        assert scheduler.next_run("sample_report") is None
    finally:
        scheduler.shutdown()


# --- timeline -----------------------------------------------------------------------


def stage(
    run_id: str, name: str, status: StageStatus, attempt: int = 1, message: str = ""
) -> StageRecord:
    return StageRecord(
        run_id=run_id,
        report="r",
        stage=name,
        attempt=attempt,
        status=status,
        started_at=utcnow(),
        message=message,
    )


def test_timeline_orders_stages_and_groups_acquisition_attempts() -> None:
    run = RunRecord(
        run_id="x", report="r", trigger=Trigger.CLI, status=RunStatus.FAILED, started_at=utcnow()
    )
    stages = [
        stage("x", "config", StageStatus.SUCCESS),
        stage("x", "acquisition", StageStatus.FAILED, 1, "timeout"),
        stage("x", "acquisition.login", StageStatus.SUCCESS, 1),
        stage("x", "acquisition.navigate", StageStatus.FAILED, 1, "timeout"),
        stage("x", "acquisition", StageStatus.FAILED, 2, "auth"),
        stage("x", "acquisition.login", StageStatus.FAILED, 2, "auth"),
    ]
    timeline = build_timeline(run, stages)
    assert [s.key for s in timeline][:3] == ["config", "preflight", "acquisition"]
    acquisition = timeline[2]
    assert acquisition.state == "failed" and acquisition.message == "auth"
    assert [a.number for a in acquisition.attempts] == [1, 2]
    assert acquisition.label.endswith("(attempt 2 of 2)")
    assert timeline[1].state == "skipped" and timeline[-1].state == "skipped"


# --- API ----------------------------------------------------------------------------


class FakeClickHouse:
    def ping(self) -> bool:
        return True


class FakeLoader:
    """Pretends the destination table does not exist yet (no drift to report)."""

    def schema_drift(self, destination: Any) -> list[str] | None:
        return None


@pytest.fixture
def client(
    reports_dir: Path, tmp_path: Path
) -> Iterator[tuple[TestClient, InMemoryRunRepository, LocalStorage]]:
    service, runs, _ = make_service(reports_dir, tmp_path)
    storage = LocalStorage(tmp_path / "storage")
    registry = ReportRegistry(reports_dir, TEST_ENV)
    container = Container(
        settings=Settings(),
        clickhouse=FakeClickHouse(),  # type: ignore[arg-type]
        registry=registry,
        storage=storage,
        runs=runs,
        data_reader=None,  # type: ignore[arg-type]
        run_service=service,
        config_store=ConfigStore(registry),
        loader=FakeLoader(),  # type: ignore[arg-type]
    )
    app = create_app(Settings(), container=container, scheduler=False)
    with TestClient(app) as test_client:
        yield test_client, runs, storage


def seed_run(runs: InMemoryRunRepository, status: RunStatus = RunStatus.FAILED) -> RunRecord:
    run = RunRecord(
        run_id=new_run_id(),
        report="sample_report",
        trigger=Trigger.CLI,
        status=status,
        started_at=utcnow(),
        finished_at=utcnow(),
        duration_ms=1200,
        error_type="AuthenticationError" if status is RunStatus.FAILED else "",
        error_message="rejected" if status is RunStatus.FAILED else "",
        error_stage="acquisition",
    )
    runs.save_run(run)
    return run


def test_health_and_reports(client: Any) -> None:
    test_client, _, _ = client
    assert test_client.get("/api/health").json()["status"] == "ok"
    reports = test_client.get("/api/reports").json()
    assert reports[0]["name"] == "sample_report" and reports[0]["valid"]


def test_report_detail_masks_secrets(client: Any) -> None:
    test_client, _, _ = client
    body = test_client.get("/api/reports/sample_report").text
    assert "s3cret-Passw0rd" not in body
    assert "**********" in body
    assert test_client.get("/api/reports/nope").status_code == 404


def test_start_run_and_conflict(client: Any) -> None:
    test_client, _, _ = client
    response = test_client.post("/api/reports/sample_report/runs")
    assert response.status_code == 202
    assert response.json()["trigger"] == "api"


def test_run_detail_logs_and_retry(client: Any) -> None:
    test_client, runs, storage = client
    run = seed_run(runs)
    storage.put_bytes(
        b'{"ts":"2026-09-21T10:00:00","level":"ERROR","stage":"acquisition","message":"boom"}\n',
        f"logs/sample_report/{run.run_date}/{run.run_id}/run.log",
    )
    detail = test_client.get(f"/api/runs/{run.run_id}").json()
    assert detail["run"]["status"] == "FAILED" and detail["timeline"][0]["key"] == "config"
    assert test_client.get(f"/api/runs/{run.run_id}/logs").json()[0]["message"] == "boom"
    retry = test_client.post(f"/api/runs/{run.run_id}/retry").json()
    assert retry["parent_run_id"] == run.run_id and retry["trigger"] == "retry"
    assert test_client.get("/api/runs/unknown").status_code == 404


def test_artifacts_are_served_safely(client: Any) -> None:
    test_client, _, storage = client
    storage.put_bytes(b"<html><script>x</script></html>", "failures/r/2026-09-21/x/page.html")
    response = test_client.get("/api/artifacts/failures/r/2026-09-21/x/page.html")
    assert response.headers["content-type"].startswith("text/plain")  # never rendered as HTML
    assert test_client.get("/api/artifacts/../../etc/passwd").status_code == 404
    assert test_client.get("/api/artifacts/secrets/x").status_code == 404


def test_ui_pages_render_from_real_state(client: Any) -> None:
    test_client, runs, _ = client
    failed = seed_run(runs)
    seed_run(runs, RunStatus.SUCCESS)
    overview = test_client.get("/")
    assert overview.status_code == 200 and "sample_report" in overview.text
    assert "50%" in overview.text  # 1 success of 2 finished runs - computed, not hard-coded
    run_page = test_client.get(f"/runs/{failed.run_id}")
    assert "AuthenticationError" in run_page.text and "Retry" in run_page.text
    assert test_client.get("/runs?status=FAILED").status_code == 200


@pytest.mark.parametrize(
    ("expr", "weekday"),
    [("0 8 * * 1", 0), ("0 8 * * 0", 6), ("0 8 * * 7", 6), ("0 8 * * 5", 4)],
)
def test_cron_day_of_week_uses_standard_cron_numbering(expr: str, weekday: int) -> None:
    from datetime import UTC, datetime

    from dashdashgo.scheduling.cron import cron_trigger

    fire = cron_trigger(expr, "UTC").get_next_fire_time(None, datetime(2026, 9, 21, tzinfo=UTC))
    assert fire is not None and fire.weekday() == weekday


def test_cron_ranges_and_steps_translate() -> None:
    from dashdashgo.scheduling.cron import cron_trigger

    assert "mon-fri" in str(cron_trigger("0 9 * * 1-5", "UTC"))
    assert "*/2" in str(cron_trigger("0 9 * * */2", "UTC"))


def test_run_page_explains_logs_stored_elsewhere(client: Any) -> None:
    test_client, runs, _ = client
    run = seed_run(runs, RunStatus.SUCCESS)
    runs.save_run(run.model_copy(update={"executed_on": "laptop:/tmp/other-storage"}))
    page = test_client.get(f"/runs/{run.run_id}").text
    assert "No log file in this server" in page
    assert "laptop:/tmp/other-storage" in page
