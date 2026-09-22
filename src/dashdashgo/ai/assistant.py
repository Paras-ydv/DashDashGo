"""What the AI assistant does: explain a failed run, and draft a config from a sample.

Both features only *suggest*. A diagnosis's config change is validated exactly
like a hand-written ``--set`` override (schema, env/host/browser-flag policy)
before it is offered, and a drafted config goes through the same checks as the
editor's Save. Nothing is applied without a person clicking a button.

Everything sent to the provider is redacted (registered secrets are masked,
credentials in the config are dumped as ``**********``).
"""

from __future__ import annotations

import html
import json
import logging
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from pydantic import BaseModel, Field

from dashdashgo.ai.client import AIClient
from dashdashgo.config.loader import ReportRegistry
from dashdashgo.config.models import ReaderOptions, ReportConfig, ReportFormat
from dashdashgo.config.store import ConfigStore
from dashdashgo.errors import (
    AIError,
    AINotConfiguredError,
    ConfigurationError,
    DashDashGoError,
    StorageError,
)
from dashdashgo.ingestion.readers import get_reader
from dashdashgo.metadata.models import RunRecord, RunStatus, utcnow
from dashdashgo.metadata.repository import RunRepository
from dashdashgo.observability.logging import redactor
from dashdashgo.storage import RunArtifacts, StorageBackend

log = logging.getLogger(__name__)

DIAGNOSIS_FILE = "ai_diagnosis.json"
MAX_SAMPLE_BYTES = 5 * 1024 * 1024
CATEGORIES = (
    "credentials",
    "dashboard_changed",
    "filters",
    "network",
    "timeout",
    "export",
    "schema_drift",
    "data_quality",
    "warehouse",
    "configuration",
    "platform_bug",
    "unknown",
)
# Paths an AI-suggested override may never touch: identity, secrets, targets.
_PROTECTED_PATHS = ("name", "source.credentials", "source.base_url", "browser.launch_args")
_OVERRIDE = re.compile(r"^[a-z_][a-z0-9_]*(\.[A-Za-z0-9_]+)+=.*$", re.DOTALL)


class Diagnosis(BaseModel):
    run_id: str
    report: str
    summary: str
    likely_cause: str
    category: str = "unknown"
    suggested_fix: str
    transient: bool = False
    """True when simply retrying later is likely to work."""
    config_overrides: list[str] = Field(default_factory=list)
    """Validated ``key.path=value`` changes for a retry; empty if none apply."""
    overrides_note: str = ""
    confidence: float = Field(default=0.5, ge=0, le=1)
    model: str
    created_at: datetime = Field(default_factory=utcnow)


@dataclass
class Draft:
    name: str
    yaml: str
    notes: list[str] = field(default_factory=list)
    problems: list[tuple[str, str]] = field(default_factory=list)
    """Validation problems left after one repair round; empty = ready to save."""
    model: str = ""


# --- prompts ---------------------------------------------------------------------

_DIAGNOSE_SYSTEM = """\
You are the on-call engineer for DashDashGo, an ETL that logs into a Metabase
dashboard with Playwright, applies filters, exports a report (CSV/XLSX/JSON),
validates it and loads it into ClickHouse. Diagnose ONE failed run from the
evidence given (run record, stage timeline, log tail, masked config, and when
available a screenshot and the visible text of the page at the moment of failure).

Reply with a JSON object with exactly these keys:
  "summary":        one sentence a non-engineer understands
  "likely_cause":   the specific root cause, citing the evidence
  "category":       one of %(categories)s
  "suggested_fix":  concrete steps (config key, dashboard change, credentials...)
  "transient":      true if simply retrying later is likely to succeed
  "config_overrides": list of "dotted.path=value" strings (values in YAML syntax)
                    that would fix the run when applied to the config, e.g.
                    "browser.navigation_timeout_ms=90000", "source.filter_mode=url",
                    "source.selectors.export_menu=Download results".
                    Only keys that exist in the config schema. Never change
                    name, source.credentials, source.base_url or browser.launch_args.
                    Empty list when the fix is not a config change.
  "confidence":     0..1
Be precise and brief. Do not invent evidence."""

_DRAFT_SYSTEM = """\
You write DashDashGo report configs (YAML). You get a sample of the exported
report file, a column profile, a complete reference config and the config JSON
schema. Produce a config for the new report that:
- keeps the reference's source section (base_url, credentials as ${...}
  references, login) - never write real secrets or new ${...} variable names;
- sets source.export.format to the sample's format;
- declares destination.columns with precise ClickHouse types that fit every
  sample value (Decimal(P,S) for money, Date/DateTime, LowCardinality(String)
  for low-cardinality text, Nullable(...) where values are missing);
- chooses destination.order_by from the columns that identify a row, and
  adds ingestion.transforms (rename_columns to snake_case, parse_numbers,
  parse_dates, ...) and quality rules where the sample calls for them;
- writes to its own table: destination.table = the report name;
- starts unscheduled (schedule.enabled: false) until someone has test-run it;
- follows the schema exactly (unknown keys are rejected).
Reply with a JSON object: {"yaml": "<the full config>", "notes": ["what the
user must still check, e.g. the dashboard id/card to export", ...]}."""


# --- the assistant -------------------------------------------------------------------


class AIAssistant:
    def __init__(
        self,
        *,
        client: AIClient | None,
        storage: StorageBackend,
        runs: RunRepository,
        registry: ReportRegistry,
        config_store: ConfigStore,
    ) -> None:
        self._client = client
        self._storage = storage
        self._runs = runs
        self._registry = registry
        self._store = config_store

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def _require(self) -> AIClient:
        if self._client is None:
            raise AINotConfiguredError(
                "the AI assistant is off: set AI_API_KEY (see README, 'AI assistant')"
            )
        return self._client

    # --- diagnosis -----------------------------------------------------------------

    def stored_diagnosis(self, run: RunRecord) -> Diagnosis | None:
        key = RunArtifacts(run.report, run.run_date, run.run_id).key("logs", DIAGNOSIS_FILE)
        try:
            return Diagnosis.model_validate_json(self._storage.read_bytes(key))
        except (StorageError, ValueError):
            return None

    def diagnose(self, run_id: str) -> Diagnosis:
        client = self._require()
        run = self._runs.get_run(run_id)
        if run is None:
            raise AIError(f"no run with id {run_id!r}")
        if run.status is not RunStatus.FAILED:
            raise AIError(f"run {run_id} did not fail (status {run.status.value})")
        evidence, screenshot = self._evidence(run)
        prompt = redactor.redact(json.dumps(evidence, indent=1, default=str))
        system = _DIAGNOSE_SYSTEM % {"categories": ", ".join(CATEGORIES)}
        completion = client.complete_json(system, prompt, images=[screenshot] if screenshot else [])
        diagnosis = self._diagnosis(run, completion.data, completion.model)
        key = RunArtifacts(run.report, run.run_date, run.run_id).key("logs", DIAGNOSIS_FILE)
        try:
            self._storage.put_bytes(diagnosis.model_dump_json(indent=2).encode(), key)
        except StorageError as exc:
            log.warning("Could not store the AI diagnosis: %s", exc.message)
        log.info("AI diagnosis of run %s (%s): %s", run_id, completion.model, diagnosis.summary)
        return diagnosis

    def _evidence(self, run: RunRecord) -> tuple[dict[str, Any], bytes | None]:
        artifacts = RunArtifacts(run.report, run.run_date, run.run_id)
        stages = [
            {
                "stage": s.stage,
                "attempt": s.attempt,
                "status": s.status.value,
                "duration_ms": s.duration_ms,
                "message": s.message,
                "details": s.details,
            }
            for s in self._runs.stages(run.run_id)
        ]
        evidence: dict[str, Any] = {
            "run": run.model_dump(
                mode="json",
                include={
                    "report",
                    "trigger",
                    "status",
                    "attempts",
                    "duration_ms",
                    "records_downloaded",
                    "records_rejected",
                    "error_type",
                    "error_stage",
                    "error_message",
                },
            ),
            "timeline": stages,
            "log_tail": self._log_tail(artifacts),
        }
        try:
            config = self._registry.load(run.report)
            evidence["config"] = yaml.safe_dump(
                config.model_dump(mode="json"), sort_keys=False, width=100
            )
        except DashDashGoError as exc:
            evidence["config"] = f"(current config does not load: {exc.message})"

        screenshot = None
        for obj in self._storage.list(artifacts.key("failures", "")):
            name = obj.key.rsplit("/", 1)[-1]
            if name.endswith(".png") and screenshot is None and obj.size < 4_000_000:
                screenshot = self._storage.read_bytes(obj.key)
            elif name.endswith(".html") and "page_text" not in evidence:
                raw = self._storage.read_bytes(obj.key).decode("utf-8", errors="replace")
                evidence["page_text"] = visible_text(raw)[:4000]
            elif name.endswith(".csv") and "rejected_rows_sample" not in evidence:
                text = self._storage.read_bytes(obj.key).decode("utf-8", errors="replace")
                evidence["rejected_rows_sample"] = "\n".join(text.splitlines()[:15])
        return evidence, screenshot

    def _log_tail(self, artifacts: RunArtifacts, lines: int = 60) -> list[str]:
        try:
            raw = self._storage.read_bytes(artifacts.key("logs", "run.log")).decode(
                "utf-8", errors="replace"
            )
        except StorageError:
            return []
        tail = []
        for line in raw.splitlines()[-lines:]:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = f"{entry.get('level', '')} [{entry.get('stage') or '-'}] {entry.get('message')}"
            if exception := entry.get("exception"):
                text += "\n" + str(exception)[-1500:]
            tail.append(text)
        return tail

    def _diagnosis(self, run: RunRecord, data: dict[str, Any], model: str) -> Diagnosis:
        def text(key: str, default: str = "") -> str:
            value = data.get(key, default)
            return str(value).strip() if value is not None else default

        category = text("category", "unknown").lower()
        try:
            confidence = min(max(float(data.get("confidence", 0.5)), 0.0), 1.0)
        except (TypeError, ValueError):
            confidence = 0.5
        raw_overrides = data.get("config_overrides") or []
        if not isinstance(raw_overrides, list):
            raw_overrides = [raw_overrides]
        overrides, note = self.vet_overrides(run.report, [str(o) for o in raw_overrides])
        return Diagnosis(
            run_id=run.run_id,
            report=run.report,
            summary=text("summary", "(no summary)"),
            likely_cause=text("likely_cause"),
            category=category if category in CATEGORIES else "unknown",
            suggested_fix=text("suggested_fix"),
            transient=bool(data.get("transient", False)),
            config_overrides=overrides,
            overrides_note=note,
            confidence=confidence,
            model=model,
        )

    def vet_overrides(self, report: str, overrides: list[str]) -> tuple[list[str], str]:
        """Keep a suggested change only if it is well-formed, allowed and validates."""
        if not overrides:
            return [], ""
        for item in overrides:
            path = item.partition("=")[0].strip()
            if not _OVERRIDE.match(item.strip()):
                return [], f"suggested change {item!r} is not key.path=value"
            if any(path == p or path.startswith(p + ".") for p in _PROTECTED_PATHS):
                return [], f"suggested change to {path} was withheld (protected setting)"
        cleaned = [o.strip() for o in overrides]
        try:
            self._registry.load(report, cleaned)
        except ConfigurationError as exc:
            return [], f"suggested change does not validate: {exc.message}"
        return cleaned, ""

    # --- drafting -----------------------------------------------------------------

    def draft(
        self, name: str, filename: str, data: bytes, *, from_report: str | None = None
    ) -> Draft:
        client = self._require()
        if len(data) > MAX_SAMPLE_BYTES:
            raise AIError(f"sample file is larger than {MAX_SAMPLE_BYTES // 1024 // 1024} MB")
        fmt = sample_format(filename)
        frame = read_sample(fmt, filename, data)
        reference_name = from_report or self._default_reference()
        reference = self._store.template(name, reference_name)
        prompt = {
            "report_name": name,
            "sample_file": filename,
            "format": fmt.value,
            "rows_in_sample": len(frame),
            "columns": profile(frame),
            "first_rows": frame.head(12).astype(str).to_dict(orient="records"),
            "reference_config": reference,
            "config_schema": ReportConfig.model_json_schema(),
        }
        completion = client.complete_json(
            _DRAFT_SYSTEM, redactor.redact(json.dumps(prompt, default=str))
        )
        text, notes = _draft_output(completion.data)
        problems = self._problems(name, text)
        if problems:
            # One repair round: the model sees exactly what the validator said.
            repair = {
                "config": text,
                "problems": [f"{loc}: {msg}" for loc, msg in problems],
                "instruction": "Fix every problem and return the full corrected config.",
            }
            completion = client.complete_json(
                _DRAFT_SYSTEM, json.dumps({**prompt, "previous_attempt": repair}, default=str)
            )
            text, more_notes = _draft_output(completion.data)
            notes += [n for n in more_notes if n not in notes]
            problems = self._problems(name, text)
        return Draft(name, text, notes, problems, completion.model)

    def _default_reference(self) -> str | None:
        names = self._registry.names()
        return "weekly_sales" if "weekly_sales" in names else (names[0] if names else None)

    def _problems(self, name: str, text: str) -> list[tuple[str, str]]:
        try:
            self._store.check(name, text)
        except ConfigurationError as exc:
            return exc.problems or [("(root)", exc.message)]
        return []


def _draft_output(data: dict[str, Any]) -> tuple[str, list[str]]:
    text = data.get("yaml")
    if not isinstance(text, str) or not text.strip():
        raise AIError("the model did not return a config")
    notes = data.get("notes") or []
    return text.strip() + "\n", [str(n) for n in notes] if isinstance(notes, list) else [str(notes)]


# --- helpers ---------------------------------------------------------------------


def sample_format(filename: str) -> ReportFormat:
    suffix = Path(filename).suffix.lower().lstrip(".")
    try:
        return ReportFormat(suffix)
    except ValueError:
        raise AIError(f"unsupported sample file {filename!r}; use .csv, .xlsx or .json") from None


def read_sample(fmt: ReportFormat, filename: str, data: bytes) -> pd.DataFrame:
    with tempfile.TemporaryDirectory(prefix="ddg-sample-") as tmp:
        path = Path(tmp) / f"sample.{fmt.value}"
        path.write_bytes(data)
        try:
            frame = get_reader(fmt).read(path, ReaderOptions())
        except DashDashGoError as exc:
            raise AIError(f"cannot read {filename}: {exc.message}") from exc
    if frame.empty or not len(frame.columns):
        raise AIError(f"{filename} has no rows to learn from")
    return frame


def profile(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Per-column facts that decide a ClickHouse type."""
    columns = []
    for name in frame.columns:
        series = frame[name]
        present = series.dropna().astype(str).map(str.strip)
        present = present[present != ""]
        columns.append(
            {
                "name": str(name),
                "missing": int(len(series) - len(present)),
                "distinct": int(present.nunique()),
                "max_length": int(present.map(len).max()) if len(present) else 0,
                "examples": present.drop_duplicates().head(6).tolist(),
            }
        )
    return columns


_SCRIPT_STYLE = re.compile(r"<(script|style|noscript|svg)\b.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


def visible_text(page_html: str) -> str:
    """Roughly what a person saw: tags, scripts and styles removed, whitespace collapsed."""
    text = _TAG.sub(" ", _SCRIPT_STYLE.sub(" ", page_html))
    return " ".join(html.unescape(text).split())
