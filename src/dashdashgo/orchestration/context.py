"""Per-run context shared by the services of one pipeline execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dashdashgo.metadata.models import RunRecord
from dashdashgo.storage import RunArtifacts


@dataclass(frozen=True)
class RunContext:
    run: RunRecord
    workdir: Path
    """Scratch directory for this run; deleted when the run ends."""
    force: bool = False
    """Load even if identical data was already ingested."""

    @property
    def run_id(self) -> str:
        return self.run.run_id

    @property
    def artifacts(self) -> RunArtifacts:
        return RunArtifacts(self.run.report, self.run.run_date, self.run.run_id)
