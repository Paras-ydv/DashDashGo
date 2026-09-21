"""Python <-> ClickHouse integration. Requires a reachable ClickHouse (make test-integration)."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from dashdashgo.config.models import DestinationConfig
from dashdashgo.errors import SchemaMismatchError, VerificationError
from dashdashgo.metadata.models import (
    RunRecord,
    RunStatus,
    StageRecord,
    StageStatus,
    Trigger,
    new_run_id,
    utcnow,
)
from dashdashgo.metadata.repository import ClickHouseRunRepository
from dashdashgo.settings import Settings
from dashdashgo.warehouse.client import ClickHouse
from dashdashgo.warehouse.loader import WarehouseLoader
from dashdashgo.warehouse.reader import ReportDataReader

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def clickhouse() -> ClickHouse:
    ch = ClickHouse(Settings())
    if not ch.ping():
        pytest.skip("ClickHouse is not reachable (set CLICKHOUSE_HOST/PORT/USER/PASSWORD)")
    return ch


@pytest.fixture(scope="module")
def database(clickhouse: ClickHouse) -> Iterator[str]:
    name = f"ddg_test_{secrets.token_hex(4)}"
    yield name
    clickhouse.command(f"DROP DATABASE IF EXISTS `{name}`")


def destination(database: str, **overrides: object) -> DestinationConfig:
    spec: dict[str, object] = {
        "database": database,
        "table": "sales",
        "order_by": ["report_date", "region"],
        "partition_by": "toYYYYMM(report_date)",
        "columns": [
            {"name": "report_date", "type": "Date"},
            {"name": "region", "type": "LowCardinality(String)"},
            {"name": "revenue", "type": "Decimal(18, 2)"},
            {"name": "note", "type": "Nullable(String)"},
            {"name": "seen_at", "type": "DateTime64(3, 'UTC')"},
        ],
    }
    spec.update(overrides)
    return DestinationConfig.model_validate(spec)


def rows(revenue: str = "10.50") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "report_date": [date(2026, 9, 14), date(2026, 9, 15)],
            "region": ["East", "West"],
            "revenue": [Decimal(revenue), Decimal("3.00")],
            "note": [None, "ok"],
            "seen_at": [datetime(2026, 9, 14, 9, 30, tzinfo=UTC)] * 2,
        },
        dtype=object,
    )


def test_prepare_creates_then_verifies_table(clickhouse: ClickHouse, database: str) -> None:
    loader = WarehouseLoader(clickhouse)
    dest = destination(database)
    assert loader.prepare(dest) == "created"
    assert loader.prepare(dest) == "verified"
    ddl = clickhouse.command(f"SHOW CREATE TABLE `{database}`.sales")
    assert "ReplacingMergeTree(_ingested_at)" in ddl and "PARTITION BY toYYYYMM(report_date)" in ddl


def test_prepare_detects_schema_drift(clickhouse: ClickHouse, database: str) -> None:
    loader = WarehouseLoader(clickhouse)
    loader.prepare(destination(database, table="drift"))
    changed = destination(
        database,
        table="drift",
        columns=[
            {"name": "report_date", "type": "Date"},
            {"name": "region", "type": "String"},
            {"name": "revenue", "type": "Float64"},
        ],
    )
    with pytest.raises(SchemaMismatchError) as info:
        loader.prepare(changed)
    assert "revenue: table has Decimal(18, 2), config says Float64" in info.value.message
    assert "unexpected column note" in info.value.message


def test_missing_table_without_create_is_an_error(clickhouse: ClickHouse, database: str) -> None:
    with pytest.raises(SchemaMismatchError, match="does not exist"):
        WarehouseLoader(clickhouse).prepare(
            destination(database, table="absent", create_table=False)
        )


def test_load_verify_and_read_back_typed_values(clickhouse: ClickHouse, database: str) -> None:
    loader = WarehouseLoader(clickhouse)
    dest = destination(database, table="typed", batch_size=1)
    loader.prepare(dest)
    run_id = new_run_id()
    assert loader.load(dest, rows(), run_id, utcnow()) == 2
    assert loader.verify(dest, run_id, 2) == 2
    with pytest.raises(VerificationError):
        loader.verify(dest, run_id, 3)

    page = ReportDataReader(clickhouse).fetch(dest, since=date(2026, 9, 15))
    assert page.total == 1 and page.date_column == "report_date"
    row = page.rows[0]
    assert row["revenue"] == Decimal("3.00") and row["note"] == "ok" and row["_run_id"] == run_id


def test_reingesting_the_same_keys_keeps_one_row_per_key(
    clickhouse: ClickHouse, database: str
) -> None:
    """Idempotency at row level: ReplacingMergeTree + FINAL returns the newest version."""
    loader = WarehouseLoader(clickhouse)
    dest = destination(database, table="replacing")
    loader.prepare(dest)
    first, second = new_run_id(), new_run_id()
    loader.load(dest, rows("10.50"), first, utcnow())
    loader.load(dest, rows("99.99"), second, utcnow() + timedelta(seconds=1))

    page = ReportDataReader(clickhouse).fetch(dest)
    assert page.total == 2
    east = next(r for r in page.rows if r["region"] == "East")
    assert east["revenue"] == Decimal("99.99") and east["_run_id"] == second


def test_retried_insert_block_is_deduplicated(clickhouse: ClickHouse, database: str) -> None:
    """An insert retried after a client-side timeout must not double the rows."""
    loader = WarehouseLoader(clickhouse)
    dest = destination(database, table="retry")
    loader.prepare(dest)
    run_id, ingested_at = new_run_id(), utcnow()
    loader.load(dest, rows(), run_id, ingested_at)
    loader.load(dest, rows(), run_id, ingested_at)  # same run, same token -> dropped by ClickHouse
    assert loader.verify(dest, run_id, 2) == 2


def test_run_repository_roundtrip(clickhouse: ClickHouse, database: str) -> None:
    repo = ClickHouseRunRepository(clickhouse, database)
    repo.ensure_schema()
    run = RunRecord(
        run_id=new_run_id(),
        report="r",
        trigger=Trigger.API,
        status=RunStatus.RUNNING,
        started_at=utcnow(),
    )
    repo.save_run(run)
    repo.save_run(
        run.model_copy(
            update={
                "status": RunStatus.SUCCESS,
                "data_hash": "abc",
                "records_inserted": 5,
                "duration_ms": 900,
            }
        )
    )
    repo.save_stage(
        StageRecord(
            run_id=run.run_id,
            report="r",
            stage="parse",
            status=StageStatus.SUCCESS,
            started_at=utcnow(),
            details={"rows": 5},
        )
    )

    stored = repo.get_run(run.run_id)
    assert stored is not None and stored.status is RunStatus.SUCCESS  # latest version wins
    assert stored.started_at.tzinfo is not None
    assert repo.stages(run.run_id)[0].details == {"rows": 5}
    assert repo.find_ingested("r", "abc") is not None and repo.find_ingested("r", "zzz") is None
    assert repo.overview(7).successful_runs == 1
    assert repo.report_stats("r").rows_inserted == 5


def test_mark_interrupted_only_touches_server_runs(clickhouse: ClickHouse, database: str) -> None:
    repo = ClickHouseRunRepository(clickhouse, database)
    repo.ensure_schema()
    server_run = RunRecord(
        run_id=new_run_id(),
        report="i",
        trigger=Trigger.SCHEDULE,
        status=RunStatus.RUNNING,
        started_at=utcnow(),
    )
    cli_run = RunRecord(
        run_id=new_run_id(),
        report="i",
        trigger=Trigger.CLI,
        status=RunStatus.RUNNING,
        started_at=utcnow(),
    )
    repo.save_run(server_run)
    repo.save_run(cli_run)
    assert repo.mark_interrupted("restart") == 1
    interrupted = repo.get_run(server_run.run_id)
    assert interrupted is not None and interrupted.status is RunStatus.FAILED
    still_running = repo.get_run(cli_run.run_id)
    assert still_running is not None and still_running.status is RunStatus.RUNNING
