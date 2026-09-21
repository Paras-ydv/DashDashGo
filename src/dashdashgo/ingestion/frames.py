"""Helpers shared by readers, transforms and coercion.

Throughout ingestion, DataFrames hold plain Python objects (``object`` dtype)
with ``None`` for missing values. Parsing into real types happens exactly once,
in coercion, driven by the destination schema. This avoids pandas' dtype
inference (leading zeros dropped from IDs, ints silently becoming floats, ...)
and makes every transform behave the same regardless of the source format.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import Any

import pandas as pd


def is_missing(value: Any) -> bool:
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return isinstance(value, str) and not value.strip()


def to_object_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with object dtype, ``None`` for nulls and clean string headers."""
    out = frame.astype(object)
    out = out.where(out.notna(), None)
    out.columns = [str(c).lstrip("﻿").strip() for c in out.columns]
    return out.reset_index(drop=True)


def map_cells(series: pd.Series, fn: Callable[[Any], Any]) -> pd.Series:
    """Element-wise map that keeps object dtype and ``None``.

    ``Series.map`` re-infers the result dtype (pandas 3 turns ``[str, None]`` into
    a string column with NaN), which would break the "None means null" contract.
    """
    return pd.Series([fn(v) for v in series.tolist()], index=series.index, dtype=object)


_NUMERIC_NOISE = re.compile(r"[\s,_$€£₹]")


def clean_number(text: str) -> str:
    """Strip formatting from a numeric string: "$1,234.50" -> "1234.50", "(12.50)" -> "-12.50"."""
    cleaned = _NUMERIC_NOISE.sub("", text.strip())
    if cleaned.startswith("(") and cleaned.endswith(")"):  # accounting negatives
        cleaned = "-" + cleaned[1:-1]
    return cleaned
