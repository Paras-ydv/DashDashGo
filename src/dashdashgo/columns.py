"""A small, explicit model of the ClickHouse column types DashDashGo supports.

Destination schemas are declared once in the report config (column name +
ClickHouse type). The same parsed type drives three things, so they can never
disagree:

* config validation   - unsupported types fail before any work starts
* type coercion       - how raw report values are parsed (ingestion/coercion.py)
* DDL / drift checks  - what the ClickHouse table must look like (warehouse/ddl.py)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class Kind(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    DECIMAL = "decimal"
    BOOL = "bool"
    DATE = "date"
    DATETIME = "datetime"


_INT_RE = re.compile(r"^(U?)Int(8|16|32|64)$")
_DECIMAL_RE = re.compile(r"^Decimal\(\s*(\d+)\s*,\s*(\d+)\s*\)$")
_DATETIME64_RE = re.compile(r"^DateTime64\(\s*(\d)\s*(?:,\s*'([^']+)'\s*)?\)$")
_DATETIME_TZ_RE = re.compile(r"^DateTime\(\s*'([^']+)'\s*\)$")
_FIXED_STRING_RE = re.compile(r"^FixedString\(\s*(\d+)\s*\)$")


@dataclass(frozen=True, slots=True)
class ColumnType:
    """A parsed ClickHouse type such as ``Nullable(Decimal(18, 2))``."""

    raw: str
    kind: Kind
    nullable: bool = False
    bits: int = 0  # integers
    signed: bool = True  # integers
    precision: int = 0  # decimals
    scale: int = 0  # decimals, DateTime64 sub-second precision
    timezone: str | None = None  # DateTime / DateTime64

    @property
    def int_bounds(self) -> tuple[int, int]:
        if self.signed:
            return -(2 ** (self.bits - 1)), 2 ** (self.bits - 1) - 1
        return 0, 2**self.bits - 1


def _unwrap(type_str: str, wrapper: str) -> tuple[str, bool]:
    prefix = f"{wrapper}("
    if type_str.startswith(prefix) and type_str.endswith(")"):
        return type_str[len(prefix) : -1].strip(), True
    return type_str, False


def parse_column_type(type_str: str) -> ColumnType:
    """Parse a ClickHouse type string; raise ``ValueError`` if unsupported."""
    raw = " ".join(type_str.split())
    inner, _ = _unwrap(raw, "LowCardinality")
    inner, nullable = _unwrap(inner, "Nullable")
    # LowCardinality(Nullable(String)) is legal ClickHouse; handle both orders.
    if not nullable:
        inner, _ = _unwrap(inner, "LowCardinality")

    if inner == "String" or _FIXED_STRING_RE.match(inner):
        return ColumnType(raw, Kind.STRING, nullable)
    if m := _INT_RE.match(inner):
        return ColumnType(raw, Kind.INTEGER, nullable, bits=int(m[2]), signed=not m[1])
    if inner in ("Float32", "Float64"):
        return ColumnType(raw, Kind.FLOAT, nullable)
    if m := _DECIMAL_RE.match(inner):
        precision, scale = int(m[1]), int(m[2])
        if not 1 <= precision <= 76 or scale > precision:
            raise ValueError(f"invalid Decimal precision/scale in {type_str!r}")
        return ColumnType(raw, Kind.DECIMAL, nullable, precision=precision, scale=scale)
    if inner == "Bool":
        return ColumnType(raw, Kind.BOOL, nullable)
    if inner in ("Date", "Date32"):
        return ColumnType(raw, Kind.DATE, nullable)
    if inner == "DateTime":
        return ColumnType(raw, Kind.DATETIME, nullable)
    if m := _DATETIME_TZ_RE.match(inner):
        return ColumnType(raw, Kind.DATETIME, nullable, timezone=m[1])
    if m := _DATETIME64_RE.match(inner):
        return ColumnType(raw, Kind.DATETIME, nullable, scale=int(m[1]), timezone=m[2])
    raise ValueError(
        f"unsupported column type {type_str!r}; supported: String, FixedString(N), "
        "(U)Int8-64, Float32/64, Decimal(P,S), Bool, Date, Date32, DateTime, DateTime64, "
        "optionally wrapped in Nullable() and/or LowCardinality()"
    )
