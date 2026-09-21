"""View models shared by the JSON API and the HTML UI.

Turns raw run/stage records and stored artifacts into the shapes both
consumers present: an ordered pipeline timeline, grouped artifacts and parsed
log lines. Keeping this here means the UI and the API can never disagree about
what happened in a run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

import yaml

from dashdashgo.config.models import ReportConfig
from dashdashgo.errors import StorageError
from dashdashgo.metadata.models import RunRecord, RunStatus, StageRecord, StageStatus
from dashdashgo.storage import AREAS, RunArtifacts, StorageBackend

StepState = Literal["success", "failed", "running", "skipped", "pending"]

PIPELINE_STAGES: list[tuple[str, str]] = [
    ("config", "Validate configuration"),
    ("preflight", "Prepare destination table"),
    ("acquisition", "Acquire report"),
    ("parse", "Parse file"),
    ("transform", "Transform"),
    ("quality", "Validate data"),
    ("dedup", "Check for duplicates"),
    ("load", "Load into ClickHouse"),
    ("verify", "Verify load"),
]

ACQUISITION_STEPS: list[tuple[str, str]] = [
    ("browser", "Start browser"),
    ("login", "Log in"),
    ("navigate", "Locate report"),
    ("filters", "Apply filters"),
    ("download", "Download"),
    ("validate", "Validate download"),
]

_STATE: dict[StageStatus, StepState] = {
    StageStatus.SUCCESS: "success",
    StageStatus.FAILED: "failed",
    StageStatus.RUNNING: "running",
    StageStatus.SKIPPED: "skipped",
}


@dataclass
class TimelineStep:
    key: str
    label: str
    state: StepState
    duration_ms: int | None = None
    message: str = ""
    artifacts: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    attempts: list[AttemptView] = field(default_factory=list)


@dataclass
class AttemptView:
    number: int
    state: StepState
    message: str
    steps: list[TimelineStep]


def _step(key: str, label: str, record: StageRecord | None, fallback: StepState) -> TimelineStep:
    if record is None:
        return TimelineStep(key, label, fallback)
    return TimelineStep(
        key=key,
        label=label,
        state=_STATE[record.status],
        duration_ms=record.duration_ms,
        message=record.message,
        artifacts=list(record.details.get("artifacts", [])),
        details={k: v for k, v in record.details.items() if k != "artifacts"},
    )


def build_timeline(run: RunRecord, stages: list[StageRecord]) -> list[TimelineStep]:
    """Ordered steps; stages that never ran are 'pending' (or 'skipped' once the run ended)."""
    not_run: StepState = "skipped" if run.status.is_terminal else "pending"
    latest: dict[str, StageRecord] = {}
    by_attempt: dict[int, dict[str, StageRecord]] = {}
    for stage in stages:
        if stage.stage.startswith("acquisition.") or stage.stage == "acquisition":
            by_attempt.setdefault(stage.attempt, {})[stage.stage] = stage
        current = latest.get(stage.stage)
        if current is None or stage.attempt >= current.attempt:
            latest[stage.stage] = stage

    timeline = []
    for key, label in PIPELINE_STAGES:
        step = _step(key, label, latest.get(key), not_run)
        if key == "acquisition":
            for number in sorted(by_attempt):
                records = by_attempt[number]
                sub = [
                    _step(f"acquisition.{k}", lbl, records.get(f"acquisition.{k}"), not_run)
                    for k, lbl in ACQUISITION_STEPS
                ]
                head = records.get("acquisition")
                state = _STATE[head.status] if head else "running"
                step.attempts.append(AttemptView(number, state, head.message if head else "", sub))
                for s in sub:
                    step.artifacts.extend(a for a in s.artifacts if a not in step.artifacts)
            if len(step.attempts) > 1:
                step.label = f"{label} (attempt {step.attempts[-1].number} of {len(step.attempts)})"
        timeline.append(step)
    return timeline


@dataclass
class ArtifactView:
    key: str
    name: str
    area: str
    size: int

    @property
    def is_image(self) -> bool:
        return self.name.lower().endswith(".png")


def list_artifacts(storage: StorageBackend, run: RunRecord) -> dict[str, list[ArtifactView]]:
    artifacts = RunArtifacts(run.report, run.run_date, run.run_id)
    grouped: dict[str, list[ArtifactView]] = {}
    for area in AREAS:
        prefix = artifacts.key(area, "")
        items = [
            ArtifactView(obj.key, obj.key.rsplit("/", 1)[-1], area, obj.size)
            for obj in storage.list(prefix)
        ]
        if items:
            grouped[area] = items
    return grouped


@dataclass
class LogLine:
    ts: str
    level: str
    stage: str
    message: str
    exception: str = ""


def read_run_log(storage: StorageBackend, run: RunRecord) -> list[LogLine]:
    key = RunArtifacts(run.report, run.run_date, run.run_id).key("logs", "run.log")
    try:
        raw = storage.read_bytes(key).decode("utf-8", errors="replace")
    except StorageError:
        return []
    lines = []
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        lines.append(
            LogLine(
                ts=entry.get("ts", ""),
                level=entry.get("level", ""),
                stage=entry.get("stage") or "",
                message=entry.get("message", ""),
                exception=entry.get("exception", ""),
            )
        )
    return lines


def masked_config(config: ReportConfig) -> dict[str, Any]:
    """Config as plain data with secrets masked (SecretStr dumps as '**********')."""
    data: dict[str, Any] = config.model_dump(mode="json")
    return data


def masked_config_yaml(config: ReportConfig) -> str:
    return yaml.safe_dump(masked_config(config), sort_keys=False, allow_unicode=True, width=100)


def status_state(status: RunStatus) -> str:
    return {
        RunStatus.SUCCESS: "success",
        RunStatus.FAILED: "failed",
        RunStatus.SKIPPED: "skipped",
        RunStatus.RUNNING: "running",
        RunStatus.QUEUED: "queued",
    }[status]
