"""Shared fixtures."""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from dashdashgo.config.loader import load_report_config
from dashdashgo.config.models import ReportConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = REPO_ROOT / "reports"

TEST_ENV = {
    "METABASE_URL": "http://metabase.test:3000",
    "METABASE_USERNAME": "analyst@example.com",
    "METABASE_PASSWORD": "s3cret-Passw0rd",
}

BASE_CONFIG: dict[str, Any] = {
    "name": "sample_report",
    "source": {
        "platform": "metabase",
        "base_url": "${METABASE_URL}",
        "credentials": {"username": "${METABASE_USERNAME}", "password": "${METABASE_PASSWORD}"},
        "location": {"collection": ["Sales"], "dashboard": "Sales Report", "card": "Weekly Sales"},
        "export": {"format": "csv"},
    },
    "ingestion": {
        "transforms": ["normalize_columns", {"rename": {"columns": {"date": "report_date"}}}],
        "quality": {"rules": [{"column": "revenue", "min": 0}]},
    },
    "destination": {
        "database": "analytics",
        "table": "sample",
        "order_by": ["report_date", "region"],
        "columns": [
            {"name": "report_date", "type": "Date"},
            {"name": "region", "type": "LowCardinality(String)"},
            {"name": "revenue", "type": "Decimal(18, 2)"},
        ],
    },
    "retry": {"max_attempts": 3, "initial_delay_seconds": 0},
}


@pytest.fixture
def config_dict() -> dict[str, Any]:
    return copy.deepcopy(BASE_CONFIG)


@pytest.fixture
def write_config(tmp_path: Path) -> Callable[[dict[str, Any]], Path]:
    def write(data: dict[str, Any]) -> Path:
        path = tmp_path / "reports" / f"{data['name']}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data))
        return path

    return write


@pytest.fixture
def make_config(
    write_config: Callable[[dict[str, Any]], Path],
) -> Callable[[dict[str, Any]], ReportConfig]:
    def make(data: dict[str, Any]) -> ReportConfig:
        return load_report_config(write_config(data), TEST_ENV)

    return make


@pytest.fixture
def sample_config(
    config_dict: dict[str, Any], make_config: Callable[[dict[str, Any]], ReportConfig]
) -> ReportConfig:
    return make_config(config_dict)
