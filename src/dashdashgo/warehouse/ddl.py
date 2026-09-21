"""DDL generation for report destination tables.

Every report table gets two lineage columns appended to its configured schema:

* ``_run_id``       which pipeline run wrote the row (audit + insert verification)
* ``_ingested_at``  the ReplacingMergeTree version: when a report window is
                    re-ingested, the newest version of each ORDER BY key wins.
"""

from __future__ import annotations

from dashdashgo.config.models import DestinationConfig

LINEAGE_COLUMNS: list[tuple[str, str, str]] = [
    ("_run_id", "String", "DashDashGo run that wrote this row"),
    ("_ingested_at", "DateTime64(3, 'UTC')", "Row version for ReplacingMergeTree"),
]

# Lets ClickHouse drop a retried insert block with an already-seen
# insert_deduplication_token (non-replicated MergeTree has this off by default).
DEDUP_WINDOW = 1000


def _quote(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def expected_columns(destination: DestinationConfig) -> list[tuple[str, str]]:
    return [(c.name, c.type) for c in destination.columns] + [
        (name, ctype) for name, ctype, _ in LINEAGE_COLUMNS
    ]


def create_table_sql(destination: DestinationConfig) -> str:
    column_lines = [
        f"    `{c.name}` {c.type}" + (f" COMMENT {_quote(c.comment)}" if c.comment else "")
        for c in destination.columns
    ]
    column_lines += [f"    `{n}` {t} COMMENT {_quote(desc)}" for n, t, desc in LINEAGE_COLUMNS]
    order_by = ", ".join(f"`{c}`" for c in destination.order_by)
    lines = [
        f"CREATE TABLE IF NOT EXISTS `{destination.database}`.`{destination.table}`",
        "(",
        ",\n".join(column_lines),
        ")",
        "ENGINE = ReplacingMergeTree(`_ingested_at`)",
    ]
    if destination.partition_by:
        lines.append(f"PARTITION BY {destination.partition_by}")
    lines.append(f"ORDER BY ({order_by})")
    lines.append(f"SETTINGS non_replicated_deduplication_window = {DEDUP_WINDOW}")
    return "\n".join(lines)


def normalize_type(type_str: str) -> str:
    """ClickHouse reports ``Decimal(18, 2)`` for a declared ``Decimal(18,2)``."""
    return "".join(type_str.split())
