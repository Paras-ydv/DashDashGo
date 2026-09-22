"""Declarative, ordered transformation steps.

A report config lists steps by name, e.g.::

    transforms:
      - normalize_columns
      - rename: {columns: {date: report_date}}
      - strip_whitespace
      - pivot: {index: [usage_date, account_id], columns: metric, values: value}

Each step is a plain function ``(DataFrame, Options) -> DataFrame`` registered
with a Pydantic options model, so unknown steps or bad options are rejected
when the config is loaded, not halfway through a run. Adding a step means
writing one function here - the pipeline itself does not change.
"""

from __future__ import annotations

import ast
import json
import operator
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, DecimalException, InvalidOperation
from typing import Any, Literal

import pandas as pd
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializeAsAny,
    ValidationError,
    field_validator,
)

from dashdashgo.errors import TransformationError
from dashdashgo.ingestion.frames import clean_number, is_missing, map_cells


class TransformOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


TransformFn = Callable[[pd.DataFrame, Any], pd.DataFrame]


@dataclass(frozen=True, slots=True)
class TransformDefinition:
    name: str
    options_model: type[TransformOptions]
    fn: TransformFn
    summary: str


TRANSFORMS: dict[str, TransformDefinition] = {}


def transform(
    name: str, options_model: type[TransformOptions] = TransformOptions
) -> Callable[[TransformFn], TransformFn]:
    def register(fn: TransformFn) -> TransformFn:
        summary = (fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else ""
        TRANSFORMS[name] = TransformDefinition(name, options_model, fn, summary)
        return fn

    return register


class TransformStep(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    options: SerializeAsAny[TransformOptions]

    def describe(self) -> str:
        """Compact human-readable form, e.g. ``rename(date→report_date)``."""

        def fmt(value: Any) -> str:
            if isinstance(value, dict):
                return ", ".join(f"{k}→{fmt(v)}" for k, v in value.items())
            if isinstance(value, list):
                return ", ".join(map(fmt, value))
            return str(value)

        opts = self.options.model_dump(exclude_defaults=True)
        if not opts:
            return self.name
        if len(opts) == 1:
            return f"{self.name}({fmt(next(iter(opts.values())))})"
        return f"{self.name}({'; '.join(f'{k}: {fmt(v)}' for k, v in opts.items())})"


def parse_transform_step(item: Any) -> TransformStep:
    """Accept ``"name"``, ``{name: {options}}`` or an existing step."""
    if isinstance(item, TransformStep):
        return item
    raw_options: Any
    if isinstance(item, str):
        name, raw_options = item, {}
    elif isinstance(item, dict) and len(item) == 1:
        name, raw_options = next(iter(item.items()))
        raw_options = raw_options or {}
    else:
        raise ValueError(f"a transform must be 'name' or {{name: options}}, got {item!r}")
    definition = TRANSFORMS.get(name)
    if definition is None:
        raise ValueError(f"unknown transform {name!r}; available: {', '.join(sorted(TRANSFORMS))}")
    try:
        options = definition.options_model.model_validate(raw_options)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'options'}: {e['msg']}" for e in exc.errors()
        )
        raise ValueError(f"invalid options for transform {name!r}: {problems}") from None
    return TransformStep(name=name, options=options)


def apply_transforms(frame: pd.DataFrame, steps: list[TransformStep]) -> pd.DataFrame:
    for position, step in enumerate(steps, start=1):
        definition = TRANSFORMS[step.name]
        try:
            frame = definition.fn(frame, step.options)
        except TransformationError:
            raise
        except (KeyError, ValueError, TypeError, NameError, SyntaxError, ArithmeticError) as exc:
            # NameError/SyntaxError come from `compute` expressions (unknown column, typo).
            raise TransformationError(f"transform #{position} '{step.name}' failed: {exc}") from exc
    return frame


def _require_columns(frame: pd.DataFrame, columns: list[str], step: str) -> None:
    if missing := [c for c in columns if c not in frame.columns]:
        raise TransformationError(
            f"'{step}' references missing columns {missing}; available: {list(frame.columns)}"
        )


def _string_cells(frame: pd.DataFrame, columns: list[str] | None) -> list[str]:
    return list(frame.columns) if columns is None else columns


# --- column naming ------------------------------------------------------------

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM = re.compile(r"[^0-9a-zA-Z]+")


def to_snake_case(name: str) -> str:
    name = _CAMEL_BOUNDARY.sub("_", name.strip().lstrip("﻿"))
    return _NON_ALNUM.sub("_", name).strip("_").lower()


@transform("normalize_columns")
def normalize_columns(frame: pd.DataFrame, _: TransformOptions) -> pd.DataFrame:
    """Convert column names to snake_case ("Units Sold" -> units_sold)."""
    renamed = [to_snake_case(str(c)) for c in frame.columns]
    if duplicates := sorted({c for c in renamed if renamed.count(c) > 1}):
        raise TransformationError(f"column names collide after normalisation: {duplicates}")
    out = frame.copy()
    out.columns = renamed
    return out


class RenameOptions(TransformOptions):
    columns: dict[str, str] = Field(min_length=1)


@transform("rename", RenameOptions)
def rename(frame: pd.DataFrame, options: RenameOptions) -> pd.DataFrame:
    """Rename columns ({old: new}); every source column must exist."""
    _require_columns(frame, list(options.columns), "rename")
    return frame.rename(columns=options.columns)


class SelectOptions(TransformOptions):
    columns: list[str] = Field(min_length=1)


@transform("drop_columns", SelectOptions)
def drop_columns(frame: pd.DataFrame, options: SelectOptions) -> pd.DataFrame:
    """Remove columns that should not be ingested."""
    _require_columns(frame, options.columns, "drop_columns")
    return frame.drop(columns=options.columns)


# --- cell cleaning ------------------------------------------------------------


class ColumnsOptions(TransformOptions):
    columns: list[str] | None = Field(default=None, description="Default: all columns")


@transform("strip_whitespace", ColumnsOptions)
def strip_whitespace(frame: pd.DataFrame, options: ColumnsOptions) -> pd.DataFrame:
    """Trim surrounding whitespace in text cells; blank cells become null."""
    columns = _string_cells(frame, options.columns)
    _require_columns(frame, columns, "strip_whitespace")
    out = frame.copy()
    for column in columns:
        out[column] = map_cells(
            out[column], lambda v: (v.strip() or None) if isinstance(v, str) else v
        )
    return out


class ChangeCaseOptions(TransformOptions):
    columns: list[str] = Field(min_length=1)
    style: Literal["lower", "upper", "title"]


@transform("change_case", ChangeCaseOptions)
def change_case(frame: pd.DataFrame, options: ChangeCaseOptions) -> pd.DataFrame:
    """Normalise casing of categorical text ("audio" -> "Audio")."""
    _require_columns(frame, options.columns, "change_case")
    out = frame.copy()
    for column in options.columns:
        out[column] = map_cells(
            out[column], lambda v: getattr(v, options.style)() if isinstance(v, str) else v
        )
    return out


class FillNullOptions(TransformOptions):
    values: dict[str, Any] = Field(min_length=1)


@transform("fill_null", FillNullOptions)
def fill_null(frame: pd.DataFrame, options: FillNullOptions) -> pd.DataFrame:
    """Replace missing values per column with a constant."""
    _require_columns(frame, list(options.values), "fill_null")
    out = frame.copy()
    for column, value in options.values.items():

        def fill(cell: Any, replacement: Any = value) -> Any:
            return replacement if is_missing(cell) else cell

        out[column] = map_cells(out[column], fill)
    return out


class DropDuplicatesOptions(TransformOptions):
    subset: list[str] | None = None
    keep: Literal["first", "last"] = "first"


@transform("drop_duplicates", DropDuplicatesOptions)
def drop_duplicates(frame: pd.DataFrame, options: DropDuplicatesOptions) -> pd.DataFrame:
    """Remove exact duplicate rows (or duplicates on a subset of columns)."""
    if options.subset:
        _require_columns(frame, options.subset, "drop_duplicates")
    hashable = frame.apply(
        lambda col: map_cells(
            col,
            lambda v: (
                json.dumps(v, sort_keys=True, default=str) if isinstance(v, dict | list) else v
            ),
        )
    )
    keep_mask = ~hashable.duplicated(subset=options.subset, keep=options.keep)
    return frame[keep_mask].reset_index(drop=True)


class FilterRowsOptions(TransformOptions):
    exclude_where: dict[str, list[Any]] = Field(
        min_length=1, description="Drop rows whose column value is in the list, e.g. totals rows"
    )


@transform("filter_rows", FilterRowsOptions)
def filter_rows(frame: pd.DataFrame, options: FilterRowsOptions) -> pd.DataFrame:
    """Drop rows by value, e.g. summary/'Total' rows some exports append."""
    _require_columns(frame, list(options.exclude_where), "filter_rows")
    drop = pd.Series(False, index=frame.index)
    for column, values in options.exclude_where.items():
        drop |= frame[column].isin(values)
    return frame[~drop].reset_index(drop=True)


# --- nested JSON --------------------------------------------------------------


class ParseJsonOptions(TransformOptions):
    columns: list[str] = Field(min_length=1)
    on_error: Literal["fail", "null"] = "fail"


@transform("parse_json", ParseJsonOptions)
def parse_json(frame: pd.DataFrame, options: ParseJsonOptions) -> pd.DataFrame:
    """Decode JSON-encoded text cells into objects (e.g. a jsonb column exported as text)."""
    _require_columns(frame, options.columns, "parse_json")
    out = frame.copy()
    for column in options.columns:

        def decode(value: Any, column: str = column) -> Any:
            if not isinstance(value, str):
                return value
            try:
                return json.loads(value)
            except json.JSONDecodeError as exc:
                if options.on_error == "null":
                    return None
                raise TransformationError(
                    f"parse_json: column '{column}' holds invalid JSON ({exc.msg}): {value[:80]!r}"
                ) from exc

        out[column] = map_cells(out[column], decode)
    return out


class FlattenOptions(TransformOptions):
    columns: list[str] | None = Field(
        default=None, description="Default: every column that contains objects"
    )
    separator: str = "_"
    max_depth: int = Field(default=5, ge=1, le=20)
    lists: Literal["json", "keep"] = Field(
        default="json", description="Serialise list values to JSON text (so they fit a String)"
    )


def _flatten_value(
    value: Any, prefix: str, sep: str, depth: int, max_depth: int, out: dict[str, Any]
) -> None:
    if isinstance(value, dict) and depth < max_depth:
        for key, child in value.items():
            _flatten_value(child, f"{prefix}{sep}{key}", sep, depth + 1, max_depth, out)
    else:
        out[prefix] = value


@transform("flatten", FlattenOptions)
def flatten(frame: pd.DataFrame, options: FlattenOptions) -> pd.DataFrame:
    """Expand nested objects into columns: {"owner": {"name": ..}} -> owner_name."""
    columns = options.columns or [
        c for c in frame.columns if any(isinstance(v, dict) for v in frame[c].tolist())
    ]
    _require_columns(frame, columns, "flatten")
    out = frame.copy()
    for column in columns:
        records: list[dict[str, Any]] = []
        for value in out[column]:
            flat: dict[str, Any] = {}
            if isinstance(value, dict):
                _flatten_value(value, column, options.separator, 0, options.max_depth, flat)
            records.append(flat)
        names = list(dict.fromkeys(k for record in records for k in record))
        expanded = pd.DataFrame(
            {n: [record.get(n) for record in records] for n in names}, index=out.index, dtype=object
        )
        if clashes := [c for c in expanded.columns if c in out.columns and c != column]:
            raise TransformationError(f"flatten: generated columns already exist: {clashes}")
        position = list(out.columns).index(column)
        out = out.drop(columns=[column])
        for offset, name in enumerate(expanded.columns):
            out.insert(position + offset, name, expanded[name])
    if options.lists == "json":
        for column in out.columns:
            out[column] = map_cells(
                out[column], lambda v: json.dumps(v) if isinstance(v, list) else v
            )
    return out


# --- reshaping ----------------------------------------------------------------


class PivotOptions(TransformOptions):
    index: list[str] = Field(min_length=1)
    columns: str
    values: str
    aggregate: Literal["none", "sum", "mean", "min", "max", "first", "last"] = Field(
        default="none", description="'none' fails on duplicate index/column pairs"
    )
    fill_value: Any = Field(default=None, description="Value for missing index/column pairs")


@transform("pivot", PivotOptions)
def pivot(frame: pd.DataFrame, options: PivotOptions) -> pd.DataFrame:
    """Long -> wide: one column per distinct value of `columns`."""
    _require_columns(frame, [*options.index, options.columns, options.values], "pivot")
    if frame.empty:
        return pd.DataFrame(columns=options.index).astype(object)
    if options.aggregate == "none":
        keys = [*options.index, options.columns]
        dupes = frame[frame.duplicated(subset=keys, keep=False)]
        if not dupes.empty:
            sample = dupes[keys].head(3).to_dict(orient="records")
            raise TransformationError(
                f"pivot: {len(dupes)} rows share the same index/column pair, e.g. {sample}. "
                "Set 'aggregate' if combining them is intended."
            )
    # groupby drops groups whose keys contain nulls; that would lose rows silently.
    if frame[[*options.index, options.columns]].isna().any().any():
        raise TransformationError(
            f"pivot: columns {[*options.index, options.columns]} contain null values"
        )
    aggfunc = "first" if options.aggregate == "none" else options.aggregate
    grouped = frame.groupby([*options.index, options.columns], sort=True)[options.values]
    values = (
        grouped.agg(aggfunc)
        if aggfunc in ("first", "last")
        else grouped.agg(lambda s: getattr(pd.to_numeric(s), aggfunc)())
    )
    wide = values.unstack(options.columns)
    if options.fill_value is not None:
        wide = wide.fillna(options.fill_value)
    wide.columns = [str(c) for c in wide.columns]
    wide = wide.reset_index()
    wide = wide.astype(object)
    return wide.where(wide.notna(), None)


@transform("parse_numbers", SelectOptions)
def parse_numbers(frame: pd.DataFrame, options: SelectOptions) -> pd.DataFrame:
    """Turn formatted numbers ("$1,234.56", "12,345", "(3.50)") into exact Decimals."""
    _require_columns(frame, options.columns, "parse_numbers")
    out = frame.copy()
    for column in options.columns:

        def parse(value: Any, column: str = column) -> Any:
            if not isinstance(value, str) or is_missing(value):
                return value
            try:
                return Decimal(clean_number(value))
            except InvalidOperation:
                raise TransformationError(
                    f"parse_numbers: column '{column}' holds a non-number: {value[:40]!r}"
                ) from None

        out[column] = map_cells(out[column], parse)
    return out


# --- derived columns ------------------------------------------------------------------
#
# Expressions come from config files that can be edited in the UI, so they are
# never handed to eval()/DataFrame.eval(). They are parsed once, checked
# against a small whitelist of syntax, and interpreted row by row in exact
# Decimal arithmetic (money stays exact; 0.1 + 0.2 == 0.3).

_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_COMPARE = {
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
}
_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Compare, ast.BoolOp, ast.Name, ast.Load,
    ast.Constant, ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or,
    *_BINARY, *_COMPARE,
)  # fmt: skip


def parse_expression(expression: str) -> ast.Expression:
    """Parse an arithmetic/comparison expression; reject anything else (calls, attributes...)."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"invalid expression {expression!r}: {exc.msg}") from None
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(
                f"expression {expression!r} uses {type(node).__name__}; only column names, "
                "numbers, + - * / % **, comparisons and and/or/not are allowed"
            )
        if isinstance(node, ast.Constant) and (
            isinstance(node.value, bool) or not isinstance(node.value, int | float)
        ):
            raise ValueError(f"expression {expression!r}: only numeric constants are allowed")
    return tree


def _number(value: Any, column: str) -> Decimal | None:
    if is_missing(value):
        return None
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    try:
        return Decimal(clean_number(str(value)))
    except InvalidOperation:
        raise TransformationError(
            f"compute: column '{column}' holds a non-number {value!r}"
        ) from None


def _interpret(node: ast.AST, row: dict[str, Any]) -> Any:
    """Evaluate one parsed expression for one row; any null operand gives null."""
    if isinstance(node, ast.Expression):
        return _interpret(node.body, row)
    if isinstance(node, ast.Constant):
        return Decimal(repr(node.value))
    if isinstance(node, ast.Name):
        if node.id not in row:
            raise TransformationError(f"compute: unknown column '{node.id}'")
        return _number(row[node.id], node.id)
    if isinstance(node, ast.UnaryOp):
        operand = _interpret(node.operand, row)
        if operand is None:
            return None
        if isinstance(node.op, ast.Not):
            return not operand
        return -operand if isinstance(node.op, ast.USub) else +operand
    if isinstance(node, ast.BinOp):
        left, right = _interpret(node.left, row), _interpret(node.right, row)
        if left is None or right is None:
            return None
        try:
            return _BINARY[type(node.op)](left, right)
        except (ZeroDivisionError, InvalidOperation, DecimalException):
            return None  # x / 0 is unknown, not infinity
    if isinstance(node, ast.Compare):
        left = _interpret(node.left, row)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _interpret(comparator, row)
            if left is None or right is None:
                return None
            if not _COMPARE[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BoolOp):
        values = [_interpret(v, row) for v in node.values]
        if any(v is None for v in values):
            return None
        return all(values) if isinstance(node.op, ast.And) else any(values)
    raise TransformationError(f"compute: unsupported syntax {type(node).__name__}")


class ComputeOptions(TransformOptions):
    columns: dict[str, str] = Field(
        min_length=1,
        description="new_column: expression over columns, e.g. 'actual - budget' or "
        "'(on_hand - reserved) <= reorder_point'",
    )

    @field_validator("columns")
    @classmethod
    def _expressions_are_safe(cls, value: dict[str, str]) -> dict[str, str]:
        for expression in value.values():
            parse_expression(expression)
        return value


@transform("compute", ComputeOptions)
def compute(frame: pd.DataFrame, options: ComputeOptions) -> pd.DataFrame:
    """Derive columns from expressions in exact Decimal arithmetic (variance = actual - budget)."""
    out = frame.copy()
    for name, expression in options.columns.items():
        tree = parse_expression(expression)
        rows = [{str(k): v for k, v in r.items()} for r in out.to_dict(orient="records")]
        out[name] = pd.Series(
            [_interpret(tree, row) for row in rows], index=out.index, dtype=object
        )
    return out
