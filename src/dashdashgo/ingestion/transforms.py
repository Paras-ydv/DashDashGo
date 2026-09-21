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

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny, ValidationError

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
        except (KeyError, ValueError, TypeError) as exc:
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


class ComputeOptions(TransformOptions):
    columns: dict[str, str] = Field(
        min_length=1, description="new_column: arithmetic expression over numeric columns"
    )


@transform("compute", ComputeOptions)
def compute(frame: pd.DataFrame, options: ComputeOptions) -> pd.DataFrame:
    """Derive columns from arithmetic expressions, e.g. variance = actual - budget."""
    out = frame.copy()
    for name, expression in options.columns.items():
        referenced = [c for c in out.columns if re.search(rf"\b{re.escape(c)}\b", expression)]
        numeric = out[referenced].apply(pd.to_numeric, errors="coerce")
        result = numeric.eval(expression, engine="python")
        out[name] = pd.Series(result, index=out.index).astype(object)
        out[name] = out[name].where(out[name].notna(), None)
    return out
