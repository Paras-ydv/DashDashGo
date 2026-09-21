"""Full workflow: Metabase -> Playwright -> download -> transform -> ClickHouse.

Requires the Docker Compose stack (seeded Metabase + ClickHouse) and a Playwright
browser: ``make test-e2e``. The shipped report configs are copied with their
destination redirected to a throwaway database, so e2e runs never touch real data.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from dashdashgo.container import Container, build_container
from dashdashgo.metadata.models import RunRecord, RunStatus
from dashdashgo.settings import Settings
from tests.conftest import REPORTS_DIR

pytestmark = pytest.mark.e2e

Mutator = Callable[[dict[str, Any]], None]


@pytest.fixture(scope="module")
def e2e_db() -> str:
    return f"ddg_e2e_{secrets.token_hex(4)}"


@pytest.fixture(scope="module")
def workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("e2e")


@pytest.fixture(scope="module")
def container(workspace: Path, e2e_db: str) -> Iterator[Container]:
    reports = workspace / "reports"
    reports.mkdir()
    settings = Settings(
        reports_dir=reports, storage_root=workspace / "storage", clickhouse_metadata_database=e2e_db
    )
    built = build_container(settings)
    if not built.clickhouse.ping():
        pytest.skip("ClickHouse not reachable")
    built.runs.ensure_schema()
    yield built
    built.run_service.shutdown()
    built.clickhouse.command(f"DROP DATABASE IF EXISTS `{e2e_db}`")


def install(
    container: Container, e2e_db: str, source: str, name: str, mutate: Mutator | None = None
) -> str:
    data = yaml.safe_load((REPORTS_DIR / f"{source}.yaml").read_text())
    data["name"] = name
    data["destination"]["database"] = e2e_db
    data["retry"] = {"max_attempts": 2, "initial_delay_seconds": 0}
    data.setdefault("browser", {})["trace"] = "off"
    if mutate:
        mutate(data)
    (container.settings.reports_dir / f"{name}.yaml").write_text(yaml.safe_dump(data))
    return name


def run(container: Container, name: str) -> RunRecord:
    return container.run_service.run_now(name).run


@pytest.mark.parametrize(
    ("source", "table", "min_rows"),
    [
        ("weekly_sales", "sales_metrics", 200),
        ("customer_usage", "daily_usage", 60),
        ("q4_budget_review", "finance_data", 54),
        ("support_tickets", "support_tickets_daily", 150),
        ("marketing_campaigns", "marketing_campaign_daily", 150),
        ("inventory_snapshot", "inventory_snapshot", 22),
        ("web_traffic_hourly", "web_traffic_hourly", 200),
        ("mrr_monthly", "mrr_monthly", 36),
    ],
)
def test_report_is_acquired_transformed_and_loaded(
    container: Container, e2e_db: str, source: str, table: str, min_rows: int
) -> None:
    name = install(container, e2e_db, source, source)
    result = run(container, name)
    assert result.status is RunStatus.SUCCESS, result.error_message
    assert result.records_inserted >= min_rows
    assert result.attempts == 1

    count = container.clickhouse.query_rows(f"SELECT count() AS n FROM `{e2e_db}`.`{table}` FINAL")[
        0
    ]["n"]
    assert count == result.records_inserted
    stages = {s.stage for s in container.runs.stages(result.run_id)}
    assert {"acquisition.login", "acquisition.download", "parse", "load", "verify"} <= stages
    assert container.storage.list(f"raw/{name}/")


def test_cleaning_and_typing_on_real_exports(container: Container, e2e_db: str) -> None:
    categories = {
        r["category"]
        for r in container.clickhouse.query_rows(
            f"SELECT DISTINCT category FROM `{e2e_db}`.sales_metrics"
        )
    }
    assert categories == {
        "Accessories",
        "Audio",
        "Displays",
        "Home Office",
    }  # trimmed + title-cased
    finance = container.clickhouse.query_rows(
        f"SELECT countIf(actual_amount IS NULL) AS pending, any(toTypeName(budget_amount)) AS t "
        f"FROM `{e2e_db}`.finance_data"
    )[0]
    assert finance["pending"] == 3 and finance["t"] == "Decimal(14, 2)"


def test_second_identical_download_is_skipped(container: Container, e2e_db: str) -> None:
    result = run(container, "weekly_sales")  # installed by the test above
    assert result.status is RunStatus.SKIPPED
    assert result.duplicate_of


def test_wrong_password_fails_once_with_screenshot(container: Container, e2e_db: str) -> None:
    def wrong_password(data: dict[str, Any]) -> None:
        data["source"]["credentials"]["password"] = "definitely-wrong-password"

    name = install(container, e2e_db, "q4_budget_review", "e2e_bad_password", wrong_password)
    result = run(container, name)
    assert (result.status, result.error_type, result.attempts) == (
        RunStatus.FAILED,
        "AuthenticationError",
        1,
    )
    shots = container.storage.list(f"screenshots/{name}/")
    assert [Path(s.key).name for s in shots] == ["attempt1_login_failure.png"]
    for obj in container.storage.list(f"failures/{name}/") + container.storage.list(
        f"logs/{name}/"
    ):
        assert b"definitely-wrong-password" not in container.storage.read_bytes(obj.key)


def test_missing_dashboard_is_not_retried(container: Container, e2e_db: str) -> None:
    def missing(data: dict[str, Any]) -> None:
        data["source"]["location"]["dashboard"] = "Sales Report (deleted)"

    result = run(container, install(container, e2e_db, "weekly_sales", "e2e_missing", missing))
    assert (result.status, result.error_type, result.attempts) == (
        RunStatus.FAILED,
        "ReportNotFoundError",
        1,
    )
    assert "Sales Report" in result.error_message  # lists what the collection does contain


def test_unknown_filter_is_a_configuration_error(container: Container, e2e_db: str) -> None:
    def bad_filter(data: dict[str, Any]) -> None:
        data["source"]["filters"] = {"usage_dat": "past7days"}

    result = run(
        container, install(container, e2e_db, "customer_usage", "e2e_bad_filter", bad_filter)
    )
    assert (result.status, result.error_type) == (RunStatus.FAILED, "ConfigurationError")
    assert "usage_dat" in result.error_message


def test_unreachable_dashboard_is_retried_then_fails(container: Container, e2e_db: str) -> None:
    def unreachable(data: dict[str, Any]) -> None:
        data["source"]["base_url"] = "http://127.0.0.1:9"  # discard port: connection refused
        data["browser"]["navigation_timeout_ms"] = 5000

    result = run(
        container, install(container, e2e_db, "weekly_sales", "e2e_unreachable", unreachable)
    )
    assert (result.status, result.error_type, result.attempts) == (
        RunStatus.FAILED,
        "NavigationError",
        2,
    )


def _stage_message(container: Container, run_id: str, stage: str) -> str:
    return next(s.message for s in container.runs.stages(run_id) if s.stage == stage)


def test_raw_files_are_stored_under_configured_names(container: Container) -> None:
    names = {Path(o.key).name for o in container.storage.list("raw/weekly_sales/")}
    assert "Weekly_Sales.csv" in names
    names = {Path(o.key).name for o in container.storage.list("raw/q4_budget_review/")}
    assert "Q4_Budget_Review.json" in names


def test_dashboard_filters_are_set_through_the_widgets(container: Container, e2e_db: str) -> None:
    """support_tickets uses filter_mode: widget - a date and a multi-select category."""
    run_id = container.runs.list_runs(report="support_tickets", limit=1)[0].run_id
    message = _stage_message(container, run_id, "acquisition.filters")
    assert message == "created_date=past30days, priority=High,Urgent via widget"
    priorities = container.clickhouse.query_rows(
        f"SELECT DISTINCT priority AS p FROM `{e2e_db}`.support_tickets_daily"
    )
    assert {r["p"] for r in priorities} == {"High", "Urgent"}


def test_relative_period_without_a_shortcut_uses_the_date_editor(
    container: Container, e2e_db: str
) -> None:
    def fourteen_days(data: dict[str, Any]) -> None:
        data["source"]["filters"] = {"usage_date": "past14days"}
        data["source"]["filter_mode"] = "widget"

    result = run(
        container, install(container, e2e_db, "customer_usage", "e2e_14_days", fourteen_days)
    )
    assert result.status is RunStatus.SUCCESS, result.error_message
    assert result.records_downloaded == 14 * 10 * 4
    assert _stage_message(container, result.run_id, "acquisition.filters").endswith("via widget")


def test_widget_value_not_offered_is_a_configuration_error(
    container: Container, e2e_db: str
) -> None:
    def critical(data: dict[str, Any]) -> None:
        data["source"]["filters"]["priority"] = ["Critical"]

    result = run(container, install(container, e2e_db, "support_tickets", "e2e_critical", critical))
    assert (result.status, result.error_type) == (RunStatus.FAILED, "ConfigurationError")
    assert "does not offer the value 'Critical'" in result.error_message
