from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from dashdashgo.errors import TransformationError
from dashdashgo.ingestion.frames import to_object_frame
from dashdashgo.ingestion.transforms import apply_transforms, parse_transform_step, to_snake_case


def run(frame: pd.DataFrame, *steps: Any) -> pd.DataFrame:
    return apply_transforms(to_object_frame(frame), [parse_transform_step(s) for s in steps])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Units Sold", "units_sold"),
        ("Account ID", "account_id"),
        ("usageDate", "usage_date"),
        ("﻿Date", "date"),
        ("Revenue ($)", "revenue"),
        ("  a--b  ", "a_b"),
    ],
)
def test_snake_case(raw: str, expected: str) -> None:
    assert to_snake_case(raw) == expected


def test_normalize_columns_rejects_collisions() -> None:
    with pytest.raises(TransformationError, match="collide"):
        run(pd.DataFrame({"A B": [1], "a_b": [2]}), "normalize_columns")


def test_rename_requires_existing_columns() -> None:
    frame = pd.DataFrame({"date": ["2026-01-01"]})
    assert list(run(frame, {"rename": {"columns": {"date": "report_date"}}}).columns) == [
        "report_date"
    ]
    with pytest.raises(TransformationError, match="missing columns"):
        run(frame, {"rename": {"columns": {"nope": "x"}}})


def test_strip_whitespace_blanks_become_null() -> None:
    out = run(pd.DataFrame({"a": ["  x ", "   ", None], "b": [1, 2, 3]}), "strip_whitespace")
    assert out["a"].tolist() == ["x", None, None]
    assert out["b"].tolist() == [1, 2, 3]


def test_change_case_and_fill_null() -> None:
    out = run(
        pd.DataFrame({"c": ["audio", "DISPLAYS", None]}),
        {"change_case": {"columns": ["c"], "style": "title"}},
        {"fill_null": {"values": {"c": "Unknown"}}},
    )
    assert out["c"].tolist() == ["Audio", "Displays", "Unknown"]


def test_drop_duplicates_exact_and_subset() -> None:
    frame = pd.DataFrame({"k": [1, 1, 2], "v": ["a", "a", "b"]})
    assert len(run(frame, "drop_duplicates")) == 2
    frame = pd.DataFrame({"k": [1, 1], "v": ["a", "b"]})
    out = run(frame, {"drop_duplicates": {"subset": ["k"], "keep": "last"}})
    assert out["v"].tolist() == ["b"]


def test_filter_rows_drops_totals() -> None:
    out = run(
        pd.DataFrame({"region": ["East", "Total"], "v": [1, 9]}),
        {"filter_rows": {"exclude_where": {"region": ["Total"]}}},
    )
    assert out["region"].tolist() == ["East"]


def test_parse_json_then_flatten_nested_objects() -> None:
    frame = pd.DataFrame(
        {
            "id": [1, 2],
            "details": [
                '{"owner": {"name": "Priya", "email": "p@x"}, '
                '"tags": ["a", "b"], "approved": true}',
                '{"owner": {"name": "Omar"}}',
            ],
        }
    )
    out = run(frame, {"parse_json": {"columns": ["details"]}}, "flatten")
    assert list(out.columns) == [
        "id",
        "details_owner_name",
        "details_owner_email",
        "details_tags",
        "details_approved",
    ]
    assert out.iloc[0]["details_tags"] == '["a", "b"]'  # lists serialised to JSON text
    assert out.iloc[1]["details_owner_email"] is None


def test_parse_json_invalid_fails_or_nulls() -> None:
    frame = pd.DataFrame({"j": ["{not json"]})
    with pytest.raises(TransformationError, match="invalid JSON"):
        run(frame, {"parse_json": {"columns": ["j"]}})
    assert run(frame, {"parse_json": {"columns": ["j"], "on_error": "null"}})["j"].tolist() == [
        None
    ]


def test_flatten_respects_max_depth() -> None:
    out = run(pd.DataFrame({"a": [{"b": {"c": {"d": 1}}}]}), {"flatten": {"max_depth": 1}})
    assert list(out.columns) == ["a_b"]
    assert out.iloc[0]["a_b"] == {"c": {"d": 1}}


def test_pivot_long_to_wide() -> None:
    frame = pd.DataFrame(
        {
            "day": ["d1", "d1", "d2", "d2"],
            "acct": ["A", "A", "A", "A"],
            "metric": ["calls", "users", "calls", "users"],
            "value": [10, 2, 11, 3],
        }
    )
    out = run(frame, {"pivot": {"index": ["day", "acct"], "columns": "metric", "values": "value"}})
    assert out.to_dict(orient="records") == [
        {"day": "d1", "acct": "A", "calls": 10, "users": 2},
        {"day": "d2", "acct": "A", "calls": 11, "users": 3},
    ]


def test_pivot_rejects_ambiguous_duplicates_unless_aggregated() -> None:
    frame = pd.DataFrame({"k": ["x", "x"], "m": ["a", "a"], "v": [1, 2]})
    with pytest.raises(TransformationError, match="share the same index"):
        run(frame, {"pivot": {"index": ["k"], "columns": "m", "values": "v"}})
    out = run(frame, {"pivot": {"index": ["k"], "columns": "m", "values": "v", "aggregate": "sum"}})
    assert out["a"].tolist() == [3]


def test_pivot_missing_combination_is_null_or_filled() -> None:
    frame = pd.DataFrame({"k": ["x", "y"], "m": ["a", "b"], "v": [1, 2]})
    out = run(frame, {"pivot": {"index": ["k"], "columns": "m", "values": "v"}})
    assert out.iloc[0]["b"] is None
    filled = run(frame, {"pivot": {"index": ["k"], "columns": "m", "values": "v", "fill_value": 0}})
    assert filled.iloc[0]["b"] == 0


def test_compute_derives_columns_and_propagates_nulls() -> None:
    out = run(
        pd.DataFrame({"actual": [110.0, None], "budget": [100, 50]}),
        {"compute": {"columns": {"variance": "actual - budget"}}},
    )
    assert out["variance"].tolist()[0] == pytest.approx(10.0)
    assert out["variance"].tolist()[1] is None


def test_errors_name_the_failing_step() -> None:
    with pytest.raises(TransformationError, match="'drop_columns'"):
        run(pd.DataFrame({"a": [1]}), {"drop_columns": {"columns": ["b"]}})


def test_describe_is_compact() -> None:
    step = parse_transform_step({"rename": {"columns": {"date": "report_date"}}})
    assert step.describe() == "rename(date→report_date)"
    assert parse_transform_step("normalize_columns").describe() == "normalize_columns"


def test_parse_numbers_handles_formatted_exports() -> None:
    from decimal import Decimal

    out = run(
        pd.DataFrame({"spend": ["$1,234.56", "(12.50)", None], "clicks": ["12,345", "7", "0"]}),
        {"parse_numbers": {"columns": ["spend", "clicks"]}},
        {"compute": {"columns": {"cpc": "spend / clicks"}}},
    )
    assert out["spend"].tolist() == [Decimal("1234.56"), Decimal("-12.50"), None]
    assert out["clicks"].tolist() == [Decimal("12345"), Decimal("7"), Decimal("0")]
    assert out["cpc"].tolist()[0] == pytest.approx(0.1, rel=1e-3)
    with pytest.raises(TransformationError, match="non-number"):
        run(pd.DataFrame({"x": ["n/a"]}), {"parse_numbers": {"columns": ["x"]}})


def test_compute_with_unknown_column_is_a_transformation_error() -> None:
    with pytest.raises(TransformationError, match="'compute'"):
        run(pd.DataFrame({"a": [1]}), {"compute": {"columns": {"b": "a + missing_column"}}})
