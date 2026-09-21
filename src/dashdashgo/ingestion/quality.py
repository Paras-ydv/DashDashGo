"""Data quality checks applied after coercion.

Row-level problems (unparseable values, rule violations) are collected per row
and handled by the configured policy (fail / drop / quarantine). Dataset-level
problems - too many bad rows, too few rows, duplicate natural keys - always
fail the run: they indicate something is wrong with the report as a whole.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pandas as pd

from dashdashgo.config.models import QualityConfig, QualityRule
from dashdashgo.errors import DataQualityError
from dashdashgo.ingestion.frames import is_missing


@dataclass
class QualityReport:
    total_rows: int
    valid_rows: int
    rejected_rows: int
    problems: Counter[str] = field(default_factory=Counter)

    @property
    def invalid_ratio(self) -> float:
        return self.rejected_rows / self.total_rows if self.total_rows else 0.0

    def summary(self, limit: int = 5) -> str:
        top = ", ".join(f"{p} x{n}" for p, n in self.problems.most_common(limit))
        return f"{self.rejected_rows}/{self.total_rows} rows rejected" + (
            f" ({top})" if top else ""
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_rows": self.total_rows,
            "valid_rows": self.valid_rows,
            "rejected_rows": self.rejected_rows,
            "problems": dict(self.problems.most_common(20)),
        }


@dataclass
class QualityOutcome:
    valid: pd.DataFrame
    rejected_positions: list[int]
    row_errors: dict[int, list[str]]
    report: QualityReport


def _bound(bound: float, value: Any) -> Decimal | float:
    """Express a numeric bound in the value's type (Decimal vs float) for exact comparison."""
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise TypeError("min/max only apply to numeric columns")
    return Decimal(str(bound)) if isinstance(value, Decimal) else bound


def _rule_violation(rule: QualityRule, value: Any) -> str | None:
    if is_missing(value):
        return f"{rule.column}: required" if rule.not_null else None
    if rule.min is not None and value < _bound(rule.min, value):
        return f"{rule.column}: below minimum {rule.min:g}"
    if rule.max is not None and value > _bound(rule.max, value):
        return f"{rule.column}: above maximum {rule.max:g}"
    if rule.allowed is not None and str(value) not in rule.allowed:
        return f"{rule.column}: value not allowed"
    if rule.pattern is not None and not re.fullmatch(rule.pattern, str(value)):
        return f"{rule.column}: does not match pattern"
    return None


def _problem_key(message: str) -> str:
    """Group messages like "revenue: not a number ('abc')" by "revenue: not a number"."""
    return message.split(" (", 1)[0]


def evaluate(
    frame: pd.DataFrame,
    row_errors: dict[int, list[str]],
    config: QualityConfig,
    unique_key: list[str],
) -> QualityOutcome:
    errors = {pos: list(msgs) for pos, msgs in row_errors.items()}
    records = frame.to_dict(orient="records")
    for rule in config.rules:
        for position, record in enumerate(records):
            if position in errors and any(
                m.startswith(f"{rule.column}:") for m in errors[position]
            ):
                continue  # value already failed coercion
            try:
                violation = _rule_violation(rule, record[rule.column])
            except (TypeError, ValueError):
                violation = f"{rule.column}: rule not applicable to value"
            if violation:
                errors.setdefault(position, []).append(violation)

    problems: Counter[str] = Counter(_problem_key(m) for msgs in errors.values() for m in msgs)
    total = len(frame)
    rejected = sorted(errors)
    report = QualityReport(total, total - len(rejected), len(rejected), problems)

    if rejected and config.on_invalid_rows == "fail":
        raise DataQualityError(f"invalid rows and on_invalid_rows=fail: {report.summary()}")
    if report.invalid_ratio > config.max_invalid_ratio:
        raise DataQualityError(
            f"{report.invalid_ratio:.1%} of rows invalid exceeds max_invalid_ratio "
            f"{config.max_invalid_ratio:.1%}: {report.summary()}"
        )

    valid = frame.drop(index=frame.index[rejected]).reset_index(drop=True)
    if len(valid) < config.min_rows:
        raise DataQualityError(f"only {len(valid)} valid rows; min_rows is {config.min_rows}")

    duplicated = valid.duplicated(subset=unique_key, keep=False)
    if duplicated.any():
        sample = valid.loc[duplicated, unique_key].head(3).to_dict(orient="records")
        raise DataQualityError(
            f"{int(duplicated.sum())} rows share a natural key {unique_key}, e.g. {sample}; "
            "add a drop_duplicates/pivot aggregate transform or fix the key"
        )
    return QualityOutcome(valid, rejected, errors, report)
