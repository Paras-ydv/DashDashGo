"""Schema-driven type coercion.

Converts raw cell values into the Python objects ClickHouse expects for each
destination column (``date``, ``Decimal``, ``int``, ...). A value that cannot
be converted does not abort the run; it is reported against its row so the
quality stage can reject/quarantine that row according to policy.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from dashdashgo.columns import ColumnType, Kind
from dashdashgo.config.models import ColumnSpec
from dashdashgo.errors import TransformationError
from dashdashgo.ingestion.frames import is_missing

# ClickHouse `Date` is stored as days since epoch in UInt16; values outside this
# range are silently wrapped by the server, so reject them here instead.
DATE_MIN, DATE_MAX = date(1970, 1, 1), date(2149, 6, 6)
DATE32_MIN, DATE32_MAX = date(1900, 1, 1), date(2299, 12, 31)

_NUMERIC_NOISE = re.compile(r"[\s,_$€£₹]")
_TRUE = {"true", "t", "yes", "y", "1"}
_FALSE = {"false", "f", "no", "n", "0"}

Converter = Callable[[Any], Any]


def _clean_number(text: str) -> str:
    cleaned = _NUMERIC_NOISE.sub("", text.strip())
    if cleaned.startswith("(") and cleaned.endswith(")"):  # accounting negatives: (12.50)
        cleaned = "-" + cleaned[1:-1]
    return cleaned


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite number")
        return Decimal(repr(value))
    if isinstance(value, str):
        try:
            result = Decimal(_clean_number(value))
        except InvalidOperation:
            raise ValueError("not a number") from None
        if not result.is_finite():
            raise ValueError("non-finite number")
        return result
    raise ValueError(f"unsupported value type {type(value).__name__}")


def _string(_: ColumnType, __: ColumnSpec) -> Converter:
    def convert(value: Any) -> str:
        if isinstance(value, float) and value.is_integer():
            return str(int(value))  # 1001.0 from a spreadsheet -> "1001"
        if isinstance(value, datetime | date):
            return value.isoformat()
        return str(value)

    return convert


def _integer(ctype: ColumnType, _: ColumnSpec) -> Converter:
    low, high = ctype.int_bounds

    def convert(value: Any) -> int:
        number = _to_decimal(value)
        if number != number.to_integral_value():
            raise ValueError("not a whole number")
        result = int(number)
        if not low <= result <= high:
            raise ValueError(f"out of range for {ctype.raw}")
        return result

    return convert


def _float(_: ColumnType, __: ColumnSpec) -> Converter:
    def convert(value: Any) -> float:
        return float(_to_decimal(value))

    return convert


def _decimal(ctype: ColumnType, _: ColumnSpec) -> Converter:
    quantum = Decimal(1).scaleb(-ctype.scale)
    max_integer_digits = ctype.precision - ctype.scale

    def convert(value: Any) -> Decimal:
        result = _to_decimal(value).quantize(quantum, rounding=ROUND_HALF_EVEN)
        if result.adjusted() + 1 > max_integer_digits and result != 0:
            raise ValueError(f"too many digits for {ctype.raw}")
        return result

    return convert


def _bool(_: ColumnType, __: ColumnSpec) -> Converter:
    def convert(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if isinstance(value, int | float) and value in (0, 1):
            text = str(int(value))
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ValueError("not a boolean")

    return convert


def _parse_datetime_text(text: str, fmt: str | None) -> datetime:
    text = text.strip()
    if fmt:
        return datetime.strptime(text, fmt)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    parsed = pd.to_datetime(text, errors="coerce")  # "September 14, 2026", "14 Sep 2026", ...
    if pd.isna(parsed):
        raise ValueError("unrecognised date/time")
    result: datetime = parsed.to_pydatetime()
    return result


def _date(ctype: ColumnType, spec: ColumnSpec) -> Converter:
    is_date32 = "Date32" in ctype.raw
    low, high = (DATE32_MIN, DATE32_MAX) if is_date32 else (DATE_MIN, DATE_MAX)

    def convert(value: Any) -> date:
        if isinstance(value, datetime):
            result = value.date()
        elif isinstance(value, date):
            result = value
        elif isinstance(value, str):
            result = _parse_datetime_text(value, spec.format).date()
        else:
            raise ValueError(f"unsupported value type {type(value).__name__}")
        if not low <= result <= high:
            raise ValueError(f"outside the range of {ctype.raw} ({low}..{high})")
        return result

    return convert


def _datetime(ctype: ColumnType, spec: ColumnSpec) -> Converter:
    tz = ZoneInfo(ctype.timezone) if ctype.timezone else UTC

    def convert(value: Any) -> datetime:
        if isinstance(value, pd.Timestamp):
            value = value.to_pydatetime()
        if isinstance(value, datetime):
            result = value
        elif isinstance(value, date):
            result = datetime(value.year, value.month, value.day)
        elif isinstance(value, str):
            result = _parse_datetime_text(value, spec.format)
        else:
            raise ValueError(f"unsupported value type {type(value).__name__}")
        # Naive values are interpreted in the column's timezone (UTC by default).
        return result.replace(tzinfo=tz) if result.tzinfo is None else result.astimezone(tz)

    return convert


_CONVERTERS: dict[Kind, Callable[[ColumnType, ColumnSpec], Converter]] = {
    Kind.STRING: _string,
    Kind.INTEGER: _integer,
    Kind.FLOAT: _float,
    Kind.DECIMAL: _decimal,
    Kind.BOOL: _bool,
    Kind.DATE: _date,
    Kind.DATETIME: _datetime,
}


@dataclass
class CoercionResult:
    frame: pd.DataFrame
    """Destination columns only, holding converted Python values (None for null)."""
    row_errors: dict[int, list[str]] = field(default_factory=dict)
    """Row position -> human readable problems."""
    dropped_columns: list[str] = field(default_factory=list)


def coerce_frame(frame: pd.DataFrame, columns: list[ColumnSpec]) -> CoercionResult:
    names = [c.name for c in columns]
    if missing := [n for n in names if n not in frame.columns]:
        raise TransformationError(
            f"destination columns missing after transforms: {missing}; "
            f"available: {list(frame.columns)}"
        )

    row_errors: dict[int, list[str]] = {}
    converted: dict[str, list[Any]] = {}
    for spec in columns:
        ctype = spec.column_type
        convert = _CONVERTERS[ctype.kind](ctype, spec)
        values: list[Any] = []
        for position, raw in enumerate(frame[spec.name].tolist()):
            if is_missing(raw):
                if not ctype.nullable:
                    row_errors.setdefault(position, []).append(f"{spec.name}: missing value")
                values.append(None)
                continue
            try:
                values.append(convert(raw))
            except ValueError as exc:
                row_errors.setdefault(position, []).append(
                    f"{spec.name}: {exc} ({str(raw)[:60]!r})"
                )
                values.append(None)
        converted[spec.name] = values

    result = pd.DataFrame(converted, columns=names, dtype=object)
    dropped = [c for c in frame.columns if c not in names]
    return CoercionResult(frame=result, row_errors=row_errors, dropped_columns=dropped)
