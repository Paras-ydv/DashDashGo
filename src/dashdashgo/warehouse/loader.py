"""Loads prepared datasets into report-specific ClickHouse tables."""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd

from dashdashgo.config.models import DestinationConfig
from dashdashgo.errors import SchemaMismatchError, VerificationError
from dashdashgo.warehouse.client import ClickHouse
from dashdashgo.warehouse.ddl import create_table_sql, expected_columns, normalize_type

log = logging.getLogger(__name__)


class WarehouseLoader:
    def __init__(self, clickhouse: ClickHouse) -> None:
        self._ch = clickhouse

    def prepare(self, destination: DestinationConfig) -> str:
        """Make sure the destination table exists and matches the configured schema.

        Runs before the browser starts, so an unreachable warehouse or a drifted
        table fails the run in seconds rather than after a full download.
        """
        self._ch.command(f"CREATE DATABASE IF NOT EXISTS `{destination.database}`")
        actual = self._table_columns(destination)
        if not actual:
            if not destination.create_table:
                raise SchemaMismatchError(
                    f"table {destination.qualified_table} does not exist and create_table is false"
                )
            self._ch.command(create_table_sql(destination))
            log.info("Created table %s", destination.qualified_table)
            return "created"
        if problems := self._drift(destination, actual):
            raise SchemaMismatchError(
                f"{destination.qualified_table} does not match its configured schema: "
                + "; ".join(problems)
                + ". Migrate the table (ALTER TABLE) or update the report config."
            )
        return "verified"

    def schema_drift(self, destination: DestinationConfig) -> list[str] | None:
        """Read-only drift check: ``None`` if the table does not exist yet, else the
        differences between the live table and the config (empty = compatible)."""
        actual = self._table_columns(destination)
        return self._drift(destination, actual) if actual else None

    def _table_columns(self, destination: DestinationConfig) -> dict[str, str]:
        rows = self._ch.query_rows(
            "SELECT name, type FROM system.columns "
            "WHERE database = {db:String} AND table = {table:String} ORDER BY position",
            {"db": destination.database, "table": destination.table},
        )
        return {row["name"]: row["type"] for row in rows}

    @staticmethod
    def _drift(destination: DestinationConfig, actual: dict[str, str]) -> list[str]:
        expected = dict(expected_columns(destination))
        problems = [f"missing column {name}" for name in expected if name not in actual]
        problems += [f"unexpected column {name}" for name in actual if name not in expected]
        problems += [
            f"{name}: table has {actual[name]}, config says {ctype}"
            for name, ctype in expected.items()
            if name in actual and normalize_type(actual[name]) != normalize_type(ctype)
        ]
        return problems

    def load(
        self,
        destination: DestinationConfig,
        frame: pd.DataFrame,
        run_id: str,
        ingested_at: datetime,
    ) -> int:
        """Insert rows in batches; return the number of rows sent.

        Each batch carries ``insert_deduplication_token = <run_id>:<batch>``. If a
        batch is retried after a timeout whose insert actually succeeded server
        side, ClickHouse discards the duplicate block instead of doubling rows.
        """
        names = destination.column_names
        total = len(frame)
        for batch_no, start in enumerate(range(0, total, destination.batch_size)):
            batch = frame.iloc[start : start + destination.batch_size]
            size = len(batch)
            columns = [batch[name].tolist() for name in names]
            columns += [[run_id] * size, [ingested_at] * size]
            self._ch.insert_columns(
                destination.qualified_table,
                columns,
                [*names, "_run_id", "_ingested_at"],
                settings={"insert_deduplication_token": f"{run_id}:{batch_no}"},
            )
            log.info(
                "Inserted batch %d (%d rows) into %s",
                batch_no + 1,
                size,
                destination.qualified_table,
            )
        return total

    def verify(self, destination: DestinationConfig, run_id: str, expected_rows: int) -> int:
        rows = self._ch.query_rows(
            f"SELECT count() AS n FROM `{destination.database}`.`{destination.table}` "
            "WHERE _run_id = {run_id:String}",
            {"run_id": run_id},
        )
        found = int(rows[0]["n"]) if rows else 0
        if found != expected_rows:
            raise VerificationError(
                f"expected {expected_rows} rows for run {run_id} in "
                f"{destination.qualified_table}, found {found}"
            )
        return found
