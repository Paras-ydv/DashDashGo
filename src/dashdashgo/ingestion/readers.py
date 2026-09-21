"""Report file readers (strategy pattern).

Each reader turns one file format into an object-dtype DataFrame of raw values.
Readers never interpret types - that is coercion's job - so the rest of the
pipeline is identical for every format.

To support a new format (TSV, Parquet, XML, ...), subclass :class:`ReportReader`
and register it in ``READERS``; nothing else changes.
"""

from __future__ import annotations

import csv
import json
import warnings
import zipfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar

import pandas as pd

from dashdashgo.config.models import ReaderOptions, ReportFormat
from dashdashgo.errors import ReportFormatError
from dashdashgo.ingestion.frames import to_object_frame


class ReportReader(ABC):
    format: ClassVar[ReportFormat]

    def read(self, path: Path, options: ReaderOptions) -> pd.DataFrame:
        if not path.is_file():
            raise ReportFormatError(f"report file not found: {path}")
        return to_object_frame(self._read(path, options))

    @abstractmethod
    def _read(self, path: Path, options: ReaderOptions) -> pd.DataFrame: ...


class CSVReader(ReportReader):
    format = ReportFormat.CSV
    _SNIFF_BYTES = 64 * 1024

    def _detect_delimiter(self, path: Path, encoding: str) -> str:
        with path.open(encoding=encoding, newline="") as handle:
            sample = handle.read(self._SNIFF_BYTES)
        try:
            return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            return ","

    def _read(self, path: Path, options: ReaderOptions) -> pd.DataFrame:
        try:
            delimiter = options.delimiter or self._detect_delimiter(path, options.encoding)
            return pd.read_csv(
                path,
                sep=delimiter,
                encoding=options.encoding,
                header=options.header_row,
                dtype=str,
                keep_default_na=False,
                na_values=[""],
            )
        except UnicodeDecodeError as exc:
            raise ReportFormatError(
                f"CSV is not valid {options.encoding} text (byte {exc.start}); "
                "set ingestion.reader.encoding"
            ) from exc
        except pd.errors.EmptyDataError as exc:
            raise ReportFormatError("CSV file has no header row") from exc
        except pd.errors.ParserError as exc:
            raise ReportFormatError(f"malformed CSV: {exc}") from exc


class XLSXReader(ReportReader):
    format = ReportFormat.XLSX

    def _read(self, path: Path, options: ReaderOptions) -> pd.DataFrame:
        if not zipfile.is_zipfile(path):
            raise ReportFormatError("file is not an XLSX workbook (not a zip container)")
        try:
            with warnings.catch_warnings():
                # Metabase workbooks have no default style; openpyxl warns and copes.
                warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")
                workbook = pd.ExcelFile(path, engine="openpyxl")
                sheet = self._select_sheet(workbook, options.sheet)
                return workbook.parse(sheet, header=options.header_row, dtype=object)
        except (zipfile.BadZipFile, KeyError, ValueError) as exc:
            raise ReportFormatError(f"cannot read XLSX workbook: {exc}") from exc

    @staticmethod
    def _select_sheet(workbook: pd.ExcelFile, wanted: str | int | None) -> str | int:
        sheets = [str(s) for s in workbook.sheet_names]
        if not sheets:
            raise ReportFormatError("workbook has no sheets")
        if wanted is None:
            return sheets[0]
        if isinstance(wanted, int):
            if wanted >= len(sheets):
                raise ReportFormatError(f"sheet index {wanted} out of range; sheets: {sheets}")
            return wanted
        if wanted not in sheets:
            raise ReportFormatError(f"sheet {wanted!r} not found; sheets: {sheets}")
        return wanted


class JSONReader(ReportReader):
    """Reads an array of objects, optionally nested under ``records_path``.

    Nested objects are preserved as dict cells; the ``flatten`` transform
    expands them into columns explicitly.
    """

    format = ReportFormat.JSON

    def _read(self, path: Path, options: ReaderOptions) -> pd.DataFrame:
        try:
            with path.open(encoding=options.encoding) as handle:
                document = json.load(handle)
        except UnicodeDecodeError as exc:
            raise ReportFormatError(f"JSON is not valid {options.encoding} text") from exc
        except json.JSONDecodeError as exc:
            raise ReportFormatError(f"malformed JSON at line {exc.lineno}: {exc.msg}") from exc

        records = self._extract_records(document, options.records_path)
        if not all(isinstance(r, dict) for r in records):
            raise ReportFormatError("JSON records must all be objects")
        return pd.DataFrame.from_records(records) if records else pd.DataFrame()

    @staticmethod
    def _extract_records(document: Any, records_path: str | None) -> list[Any]:
        node = document
        for part in records_path.split(".") if records_path else []:
            if not isinstance(node, dict) or part not in node:
                raise ReportFormatError(f"records_path '{records_path}' not found (at '{part}')")
            node = node[part]
        if isinstance(node, list):
            return node
        hint = (
            f"; top-level keys: {sorted(node)[:10]} - set ingestion.reader.records_path"
            if isinstance(node, dict)
            else ""
        )
        raise ReportFormatError(
            f"expected a JSON array of records, got {type(node).__name__}{hint}"
        )


READERS: dict[ReportFormat, ReportReader] = {
    reader.format: reader for reader in (CSVReader(), XLSXReader(), JSONReader())
}


def get_reader(fmt: ReportFormat) -> ReportReader:
    try:
        return READERS[fmt]
    except KeyError:
        raise ReportFormatError(f"no reader registered for format '{fmt}'") from None
