"""Ingestion: downloaded file -> validated, typed dataset ready for loading.

The three public methods map one-to-one onto pipeline stages (parse,
transform, quality) so the orchestrator can time and record each separately.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from dashdashgo.config.models import IngestionConfig, ReportConfig, ReportFormat
from dashdashgo.errors import DataQualityError, SchemaDriftError
from dashdashgo.ingestion.coercion import coerce_frame
from dashdashgo.ingestion.quality import QualityReport, evaluate
from dashdashgo.ingestion.readers import get_reader
from dashdashgo.ingestion.transforms import apply_transforms

log = logging.getLogger(__name__)


@dataclass
class PreparedDataset:
    frame: pd.DataFrame
    """Valid rows only; columns in destination order; typed Python values."""
    rejected: pd.DataFrame
    """Rejected rows with their pre-coercion values and an `_errors` column."""
    quality: QualityReport
    fingerprint: str


def dataset_fingerprint(frame: pd.DataFrame) -> str:
    """Order-independent SHA-256 of the dataset content.

    Two downloads of the same report data produce the same fingerprint even if
    the files differ byte-wise (XLSX embeds timestamps) or rows come back in a
    different order. That makes it the right key for duplicate detection.
    """
    canonical = frame.astype(str)
    canonical = canonical.sort_values(by=list(canonical.columns), kind="stable")
    digest = hashlib.sha256("\x1f".join(canonical.columns).encode())
    digest.update(canonical.to_csv(index=False, header=False).encode())
    return digest.hexdigest()


def _rows_with_errors(frame: pd.DataFrame, row_errors: dict[int, list[str]]) -> pd.DataFrame:
    """The original (pre-coercion) values of the given rows plus an `_errors` column."""
    positions = sorted(row_errors)
    rows = frame.iloc[positions].copy()
    rows["_errors"] = ["; ".join(row_errors[p]) for p in positions]
    return rows.reset_index(drop=True)


class IngestionService:
    def parse(self, path: Path, fmt: ReportFormat, config: IngestionConfig) -> pd.DataFrame:
        frame = get_reader(fmt).read(path, config.reader)
        if missing := [c for c in config.expected_columns if c not in frame.columns]:
            raise SchemaDriftError(
                f"downloaded report is missing expected columns {missing}; "
                f"found: {list(frame.columns)}"
            )
        log.info("Parsed %s file: %d rows x %d columns", fmt.value.upper(), *frame.shape)
        return frame

    def transform(self, frame: pd.DataFrame, config: IngestionConfig) -> pd.DataFrame:
        result = apply_transforms(frame, config.transforms)
        log.info(
            "Applied %d transform(s): %d -> %d rows, columns: %s",
            len(config.transforms),
            len(frame),
            len(result),
            ", ".join(map(str, result.columns)),
        )
        return result

    def validate(self, frame: pd.DataFrame, report: ReportConfig) -> PreparedDataset:
        coerced = coerce_frame(frame, report.destination.columns)
        if coerced.dropped_columns:
            log.info("Ignoring columns not in destination schema: %s", coerced.dropped_columns)
        try:
            outcome = evaluate(
                coerced.frame, coerced.row_errors, report.ingestion.quality, report.unique_key
            )
        except DataQualityError as exc:
            # Nothing is loaded, but keep the offending rows for inspection.
            exc.rejected = _rows_with_errors(frame, exc.row_errors)
            raise
        rejected = _rows_with_errors(frame, outcome.row_errors)
        if outcome.report.rejected_rows:
            log.warning("Data quality: %s", outcome.report.summary())
        log.info("Data quality: %d valid rows", outcome.report.valid_rows)
        return PreparedDataset(
            frame=outcome.valid,
            rejected=rejected.reset_index(drop=True),
            quality=outcome.report,
            fingerprint=dataset_fingerprint(outcome.valid),
        )
