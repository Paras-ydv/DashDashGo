from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest

from dashdashgo.columns import Kind, parse_column_type
from dashdashgo.config.models import ColumnSpec, QualityConfig, QualityRule
from dashdashgo.errors import DataQualityError, TransformationError
from dashdashgo.ingestion.coercion import coerce_frame
from dashdashgo.ingestion.quality import evaluate
from dashdashgo.ingestion.service import dataset_fingerprint


@pytest.mark.parametrize(
    ("type_str", "kind", "nullable"),
    [
        ("String", Kind.STRING, False),
        ("LowCardinality(String)", Kind.STRING, False),
        ("LowCardinality(Nullable(String))", Kind.STRING, True),
        ("Nullable(Decimal(14, 2))", Kind.DECIMAL, True),
        ("UInt32", Kind.INTEGER, False),
        ("DateTime64(3, 'UTC')", Kind.DATETIME, False),
        ("Date32", Kind.DATE, False),
        ("Bool", Kind.BOOL, False),
    ],
)
def test_parse_column_type(type_str: str, kind: Kind, nullable: bool) -> None:
    parsed = parse_column_type(type_str)
    assert (parsed.kind, parsed.nullable) == (kind, nullable)


def test_parse_column_type_rejects_unsupported() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        parse_column_type("Map(String, UInt8)")


def coerce_one(
    type_str: str, values: list[Any], fmt: str | None = None
) -> tuple[list[Any], dict[int, list[str]]]:
    spec = ColumnSpec(name="c", type=type_str, format=fmt)
    result = coerce_frame(pd.DataFrame({"c": values}, dtype=object), [spec])
    return result.frame["c"].tolist(), result.row_errors


def test_integers_accept_clean_numbers_and_reject_the_rest() -> None:
    values, errors = coerce_one("UInt32", ["1,234", 5.0, " 7 ", "1.5", "-1", "abc", 4294967296])
    assert values[:3] == [1234, 5, 7]
    assert set(errors) == {3, 4, 5, 6}
    assert "not a whole number" in errors[3][0]
    assert "out of range" in errors[4][0]


def test_decimals_are_exact_and_bounded() -> None:
    values, errors = coerce_one("Decimal(6, 2)", ["$1,234.567", 0.1, "(12.50)", "123456"])
    assert values[:3] == [Decimal("1234.57"), Decimal("0.10"), Decimal("-12.50")]
    assert "too many digits" in errors[3][0]


def test_dates_from_iso_text_datetime_and_locale_format() -> None:
    values, errors = coerce_one(
        "Date", ["2026-09-14", datetime(2026, 9, 15, 10), "September 16, 2026", "31/31/2026"]
    )
    assert values[:3] == [date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)]
    assert 3 in errors


def test_date_outside_clickhouse_range_is_rejected() -> None:
    _, errors = coerce_one("Date", ["1969-12-31"])
    assert "outside the range" in errors[0][0]


def test_explicit_date_format() -> None:
    values, _ = coerce_one("Date", ["14/09/2026"], fmt="%d/%m/%Y")
    assert values == [date(2026, 9, 14)]


def test_datetimes_are_normalised_to_column_timezone() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    values, _ = coerce_one(
        "DateTime('UTC')", ["2025-09-21T09:30:00Z", datetime(2025, 1, 1, 9, 30, tzinfo=ist)]
    )
    assert values == [
        datetime(2025, 9, 21, 9, 30, tzinfo=UTC),
        datetime(2025, 1, 1, 4, 0, tzinfo=UTC),
    ]


def test_nulls_respect_nullability() -> None:
    values, errors = coerce_one("Nullable(Float64)", [None, "", "2.5"])
    assert values == [None, None, 2.5] and not errors
    _, errors = coerce_one("Float64", [None])
    assert errors == {0: ["c: missing value"]}


def test_strings_from_spreadsheet_numbers_and_booleans() -> None:
    assert coerce_one("String", [1001.0, "x"])[0] == ["1001", "x"]
    assert coerce_one("Bool", ["yes", "0", True])[0] == [True, False, True]


def test_missing_destination_column_fails_fast() -> None:
    with pytest.raises(TransformationError, match="missing after transforms"):
        coerce_frame(pd.DataFrame({"a": [1]}), [ColumnSpec(name="b", type="String")])


def test_extra_columns_are_dropped_and_reported() -> None:
    result = coerce_frame(
        pd.DataFrame({"a": ["1"], "extra": ["x"]}), [ColumnSpec(name="a", type="String")]
    )
    assert list(result.frame.columns) == ["a"]
    assert result.dropped_columns == ["extra"]


# --- quality ------------------------------------------------------------------------


def frame(**cols: list[Any]) -> pd.DataFrame:
    return pd.DataFrame(cols, dtype=object)


def test_rules_reject_rows_and_count_problems() -> None:
    data = frame(
        k=[1, 2, 3, 4],
        revenue=[Decimal("5"), Decimal("-1"), Decimal("2"), Decimal("3")],
        region=["East", "East", "Mars", "West"],
    )
    config = QualityConfig(
        max_invalid_ratio=0.5,
        rules=[
            QualityRule(column="revenue", min=0),
            QualityRule(column="region", allowed=["East", "West"]),
        ],
    )
    outcome = evaluate(data, {}, config, ["k"])
    assert outcome.rejected_positions == [1, 2]
    assert outcome.valid["k"].tolist() == [1, 4]
    assert outcome.report.problems == {
        "revenue: below minimum 0": 1,
        "region: value not allowed": 1,
    }


def test_policy_fail_aborts_on_any_invalid_row() -> None:
    with pytest.raises(DataQualityError, match="on_invalid_rows=fail"):
        evaluate(frame(k=[1, 2]), {1: ["x: bad"]}, QualityConfig(on_invalid_rows="fail"), ["k"])


def test_invalid_ratio_threshold() -> None:
    with pytest.raises(DataQualityError, match="exceeds max_invalid_ratio"):
        evaluate(frame(k=[1, 2]), {1: ["x: bad"]}, QualityConfig(max_invalid_ratio=0.1), ["k"])


def test_duplicate_natural_keys_fail() -> None:
    with pytest.raises(DataQualityError, match="share a natural key"):
        evaluate(frame(k=[1, 1], v=[1, 2]), {}, QualityConfig(), ["k"])


def test_min_rows() -> None:
    with pytest.raises(DataQualityError, match="min_rows"):
        evaluate(frame(k=[]), {}, QualityConfig(min_rows=1), ["k"])


def test_pattern_and_not_null_rules() -> None:
    config = QualityConfig(
        max_invalid_ratio=1,
        rules=[
            QualityRule(column="id", pattern=r"ACC-\d{4}"),
            QualityRule(column="owner", not_null=True),
        ],
    )
    outcome = evaluate(frame(id=["ACC-1001", "X-1"], owner=["a", None]), {}, config, ["id"])
    assert outcome.rejected_positions == [1]
    assert sorted(outcome.row_errors[1]) == ["id: does not match pattern", "owner: required"]


def test_fingerprint_ignores_row_order_but_not_content() -> None:
    a = frame(k=[1, 2], v=["x", "y"])
    b = frame(k=[2, 1], v=["y", "x"])
    c = frame(k=[1, 2], v=["x", "z"])
    assert dataset_fingerprint(a) == dataset_fingerprint(b)
    assert dataset_fingerprint(a) != dataset_fingerprint(c)
