"""Read access to ingested report data (used by the distribution API)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from dashdashgo.columns import Kind
from dashdashgo.config.models import DestinationConfig
from dashdashgo.warehouse.client import ClickHouse


@dataclass
class DataPage:
    columns: list[str]
    rows: list[dict[str, Any]]
    total: int
    limit: int
    offset: int
    date_column: str | None


def primary_date_column(destination: DestinationConfig) -> str | None:
    """The first Date/DateTime column of the sort key (or schema) - used for range filters."""
    by_name = {c.name: c for c in destination.columns}
    candidates = [by_name[n] for n in destination.order_by] + destination.columns
    for column in candidates:
        if column.column_type.kind in (Kind.DATE, Kind.DATETIME):
            return column.name
    return None


class ReportDataReader:
    def __init__(self, clickhouse: ClickHouse) -> None:
        self._ch = clickhouse

    def fetch(
        self,
        destination: DestinationConfig,
        *,
        limit: int = 100,
        offset: int = 0,
        since: date | None = None,
        until: date | None = None,
        run_id: str | None = None,
    ) -> DataPage:
        """Return the current (deduplicated) state of the table.

        ``FINAL`` applies ReplacingMergeTree's deduplication at read time, so
        consumers always see one row per natural key even before background
        merges have run.
        """
        date_column = primary_date_column(destination)
        conditions: list[str] = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if date_column and since:
            conditions.append(f"`{date_column}` >= {{since:Date}}")
            params["since"] = since
        if date_column and until:
            conditions.append(f"`{date_column}` <= {{until:Date}}")
            params["until"] = until
        if run_id:
            conditions.append("_run_id = {run_id:String}")
            params["run_id"] = run_id
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        table = f"`{destination.database}`.`{destination.table}` FINAL"
        order = ", ".join(f"`{c}`" for c in destination.order_by)
        columns = [*destination.column_names, "_run_id", "_ingested_at"]
        select = ", ".join(f"`{c}`" for c in columns)

        total_rows = self._ch.query_rows(f"SELECT count() AS n FROM {table} {where}", params)
        rows = self._ch.query_rows(
            f"SELECT {select} FROM {table} {where} ORDER BY {order} "
            "LIMIT {limit:UInt32} OFFSET {offset:UInt32}",
            params,
        )
        return DataPage(columns, rows, int(total_rows[0]["n"]), limit, offset, date_column)
