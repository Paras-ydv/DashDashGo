"""Persistence of run metadata.

Stored in ClickHouse next to the data it describes (no extra database to run).
Runs and stages change state over their lifetime; ClickHouse has no cheap
UPDATE, so each state change inserts a new row version into a
ReplacingMergeTree and reads use ``FINAL`` to see the latest version. At the
volume of pipeline metadata (thousands of rows) this is effectively free.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import timedelta
from typing import Any

from dashdashgo.metadata.models import (
    OverviewStats,
    ReportStats,
    RunRecord,
    RunStatus,
    StageRecord,
    Trigger,
    utcnow,
)
from dashdashgo.warehouse.client import ClickHouse


class RunRepository(ABC):
    @abstractmethod
    def ensure_schema(self) -> None: ...

    @abstractmethod
    def save_run(self, run: RunRecord) -> None: ...

    @abstractmethod
    def save_stage(self, stage: StageRecord) -> None: ...

    @abstractmethod
    def get_run(self, run_id: str) -> RunRecord | None: ...

    @abstractmethod
    def list_runs(
        self,
        *,
        report: str | None = None,
        status: RunStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RunRecord]: ...

    @abstractmethod
    def stages(self, run_id: str) -> list[StageRecord]: ...

    @abstractmethod
    def find_ingested(self, report: str, data_hash: str) -> RunRecord | None:
        """Latest successful run of ``report`` that loaded data with this fingerprint."""

    @abstractmethod
    def overview(self, window_days: int) -> OverviewStats: ...

    @abstractmethod
    def report_stats(self, report: str, recent: int = 20) -> ReportStats: ...

    @abstractmethod
    def mark_interrupted(self, message: str) -> int:
        """Fail QUEUED/RUNNING runs that were executing inside the (restarted) server."""


_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS `{db}`.pipeline_runs
(
    run_id             String,
    report             LowCardinality(String),
    trigger            LowCardinality(String),
    status             LowCardinality(String),
    parent_run_id      Nullable(String),
    started_at         DateTime64(3, 'UTC'),
    finished_at        Nullable(DateTime64(3, 'UTC')),
    duration_ms        Nullable(UInt64),
    current_stage      LowCardinality(String),
    attempts           UInt8,
    records_downloaded UInt64,
    records_rejected   UInt64,
    records_inserted   UInt64,
    destination_table  String,
    source_file        String,
    file_hash          String,
    data_hash          String,
    duplicate_of       Nullable(String),
    error_type         LowCardinality(String),
    error_message      String,
    error_stage        LowCardinality(String),
    updated_at         DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY run_id
"""

_STAGES_DDL = """
CREATE TABLE IF NOT EXISTS `{db}`.pipeline_stage_events
(
    run_id      String,
    report      LowCardinality(String),
    stage       LowCardinality(String),
    attempt     UInt8,
    status      LowCardinality(String),
    started_at  DateTime64(3, 'UTC'),
    finished_at Nullable(DateTime64(3, 'UTC')),
    duration_ms Nullable(UInt64),
    message     String,
    details     String,
    updated_at  DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (run_id, stage, attempt)
"""

_RUN_FIELDS = list(RunRecord.model_fields)
_STAGE_FIELDS = list(StageRecord.model_fields)


class ClickHouseRunRepository(RunRepository):
    def __init__(self, clickhouse: ClickHouse, database: str) -> None:
        self._ch = clickhouse
        self._db = database

    @property
    def _runs(self) -> str:
        return f"`{self._db}`.pipeline_runs"

    @property
    def _stages(self) -> str:
        return f"`{self._db}`.pipeline_stage_events"

    def ensure_schema(self) -> None:
        self._ch.command(f"CREATE DATABASE IF NOT EXISTS `{self._db}`")
        self._ch.command(_RUNS_DDL.format(db=self._db))
        self._ch.command(_STAGES_DDL.format(db=self._db))

    # --- writes ---------------------------------------------------------------

    def save_run(self, run: RunRecord) -> None:
        run = run.model_copy(update={"updated_at": utcnow()})
        row = run.model_dump()
        self._ch.insert_columns(self._runs, [[row[f]] for f in _RUN_FIELDS], _RUN_FIELDS)

    def save_stage(self, stage: StageRecord) -> None:
        stage = stage.model_copy(update={"updated_at": utcnow()})
        row = stage.model_dump()
        row["details"] = json.dumps(row["details"], default=str)
        self._ch.insert_columns(self._stages, [[row[f]] for f in _STAGE_FIELDS], _STAGE_FIELDS)

    # --- reads ----------------------------------------------------------------

    @staticmethod
    def _run(row: dict[str, Any]) -> RunRecord:
        return RunRecord.model_validate(row)

    def get_run(self, run_id: str) -> RunRecord | None:
        rows = self._ch.query_rows(
            f"SELECT * FROM {self._runs} FINAL WHERE run_id = {{run_id:String}}", {"run_id": run_id}
        )
        return self._run(rows[0]) if rows else None

    def list_runs(
        self,
        *,
        report: str | None = None,
        status: RunStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RunRecord]:
        conditions: list[str] = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if report:
            conditions.append("report = {report:String}")
            params["report"] = report
        if status:
            conditions.append("status = {status:String}")
            params["status"] = status.value
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self._ch.query_rows(
            f"SELECT * FROM {self._runs} FINAL {where} ORDER BY started_at DESC "
            "LIMIT {limit:UInt32} OFFSET {offset:UInt32}",
            params,
        )
        return [self._run(r) for r in rows]

    def stages(self, run_id: str) -> list[StageRecord]:
        rows = self._ch.query_rows(
            f"SELECT * FROM {self._stages} FINAL WHERE run_id = {{run_id:String}} "
            "ORDER BY started_at, attempt",
            {"run_id": run_id},
        )
        for row in rows:
            row["details"] = json.loads(row["details"] or "{}")
        return [StageRecord.model_validate(r) for r in rows]

    def find_ingested(self, report: str, data_hash: str) -> RunRecord | None:
        rows = self._ch.query_rows(
            f"SELECT * FROM {self._runs} FINAL WHERE report = {{report:String}} "
            "AND data_hash = {hash:String} AND status = 'SUCCESS' "
            "ORDER BY started_at DESC LIMIT 1",
            {"report": report, "hash": data_hash},
        )
        return self._run(rows[0]) if rows else None

    def overview(self, window_days: int) -> OverviewStats:
        rows = self._ch.query_rows(
            f"""
            SELECT
                count()                                   AS total_runs,
                countIf(status = 'SUCCESS')               AS successful_runs,
                countIf(status = 'FAILED')                AS failed_runs,
                countIf(status = 'SKIPPED')               AS skipped_runs,
                countIf(status IN ('RUNNING', 'QUEUED'))  AS running_runs,
                sum(records_inserted)                     AS rows_inserted,
                sum(records_rejected)                     AS rows_rejected,
                medianIf(duration_ms, status IN ('SUCCESS', 'SKIPPED')) AS median_duration_ms
            FROM {self._runs} FINAL
            WHERE started_at >= {{since:DateTime64(3)}}
            """,
            {"since": utcnow() - timedelta(days=window_days)},
        )
        stats = rows[0]
        median = stats.pop("median_duration_ms")
        return OverviewStats(
            window_days=window_days,
            median_duration_ms=None if median is None or median != median else float(median),
            **{k: int(v or 0) for k, v in stats.items()},
        )

    def report_stats(self, report: str, recent: int = 20) -> ReportStats:
        rows = self._ch.query_rows(
            f"""
            SELECT
                count()                        AS total_runs,
                countIf(status = 'SUCCESS')    AS successful_runs,
                countIf(status = 'FAILED')     AS failed_runs,
                countIf(status = 'SKIPPED')    AS skipped_runs,
                sum(records_inserted)          AS rows_inserted,
                avgIf(duration_ms, status = 'SUCCESS') AS avg_duration_ms
            FROM {self._runs} FINAL WHERE report = {{report:String}}
            """,
            {"report": report},
        )
        stats = rows[0]
        avg = stats.pop("avg_duration_ms")
        latest = self.list_runs(report=report, limit=recent)
        return ReportStats(
            report=report,
            avg_duration_ms=None if avg is None or avg != avg else float(avg),
            last_run=latest[0] if latest else None,
            recent_runs=list(reversed(latest)),
            **{k: int(v or 0) for k, v in stats.items()},
        )

    def mark_interrupted(self, message: str) -> int:
        server_triggers = [t.value for t in Trigger if t.executes_in_server]
        rows = self._ch.query_rows(
            f"SELECT * FROM {self._runs} FINAL WHERE status IN ('QUEUED', 'RUNNING') "
            "AND trigger IN {triggers:Array(String)}",
            {"triggers": server_triggers},
        )
        now = utcnow()
        for row in rows:
            run = self._run(row)
            self.save_run(
                run.model_copy(
                    update={
                        "status": RunStatus.FAILED,
                        "finished_at": now,
                        "duration_ms": int((now - run.started_at).total_seconds() * 1000),
                        "error_type": "Interrupted",
                        "error_message": message,
                        "error_stage": run.current_stage or "queued",
                    }
                )
            )
        return len(rows)
