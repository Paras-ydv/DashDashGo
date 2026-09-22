"""CLI: argument parsing, run overrides and the config commands (no infrastructure needed)."""

from __future__ import annotations

import argparse
import io
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from dashdashgo.cli import build_parser, main
from dashdashgo.cli.output import table
from dashdashgo.cli.runs import _run_overrides
from dashdashgo.settings import get_settings
from tests.conftest import REPORTS_DIR, TEST_ENV


@pytest.fixture
def reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    directory = tmp_path / "reports"
    shutil.copytree(REPORTS_DIR, directory)
    monkeypatch.setenv("REPORTS_DIR", str(directory))
    monkeypatch.setenv("NO_COLOR", "1")
    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield directory
    get_settings.cache_clear()


def parse(*argv: str) -> argparse.Namespace:
    return build_parser().parse_args(list(argv))


def test_run_flags_become_config_overrides() -> None:
    args = parse(
        "run",
        "weekly_sales",
        "--set",
        "source.filters.region=East",
        "--headed",
        "--no-retry",
        "--timeout",
        "90",
    )
    assert _run_overrides(args) == [
        "source.filters.region=East",
        "browser.headless=false",
        "retry.max_attempts=1",
        "browser.timeout_ms=90000",
    ]


def test_every_command_is_registered() -> None:
    parser = build_parser()
    commands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices
    expected = {
        "run",
        "runs",
        "show",
        "logs",
        "retry",
        "data",
        "stats",
        "list",
        "validate",
        "schema",
        "config",
        "serve",
        "init-db",
        "prune",
    }
    assert expected <= set(commands)


def test_validate_and_list(reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate"]) == 0
    assert "✓ weekly_sales: CSV -> analytics.sales_metrics" in capsys.readouterr().out
    (reports / "broken.yaml").write_text("name: broken\n")
    assert main(["validate", "broken"]) == 1
    assert "source: Field required" in capsys.readouterr().err
    assert main(["list"]) == 1  # an invalid config makes `list` fail too
    assert "INVALID" in capsys.readouterr().out


def test_schema_prints_ddl(reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["schema", "q4_budget_review"]) == 0
    assert "ENGINE = ReplacingMergeTree(`_ingested_at`)" in capsys.readouterr().out


def test_config_new_history_restore_archive(
    reports: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert main(["config", "new", "sales_copy", "--from", "weekly_sales"]) == 0
    assert (reports / "sales_copy.yaml").read_text().splitlines()[3] == "name: sales_copy"

    original = (reports / "sales_copy.yaml").read_text()
    changed = original.replace("max_attempts: 3", "max_attempts: 2")
    monkeypatch.setattr("sys.stdin", io.StringIO(changed))
    assert main(["config", "import", "-", "--name", "sales_copy"]) == 0
    assert (reports / "sales_copy.yaml").read_text() == changed

    capsys.readouterr()
    assert main(["config", "history", "sales_copy"]) == 0
    version = capsys.readouterr().out.splitlines()[2].split()[0]
    assert main(["config", "restore", "sales_copy", version]) == 0
    assert (reports / "sales_copy.yaml").read_text() == original

    assert main(["config", "archive", "sales_copy", "--yes"]) == 0
    assert not (reports / "sales_copy.yaml").exists()


def test_config_import_rejects_plaintext_secrets(reports: Path, tmp_path: Path) -> None:
    leaked = tmp_path / "leaked.yaml"
    text = (reports / "weekly_sales.yaml").read_text().replace("${METABASE_PASSWORD}", "hunter2")
    leaked.write_text(text.replace("name: weekly_sales", "name: leaked"))
    assert main(["config", "import", str(leaked)]) == 2
    assert not (reports / "leaked.yaml").exists()


def test_config_show_prints_raw_yaml(reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "show", "weekly_sales"]) == 0
    assert "password: ${METABASE_PASSWORD}" in capsys.readouterr().out


def test_table_alignment() -> None:
    rendered = table(["NAME", "ROWS"], [["a", 5], ["bbb", 1234]]).splitlines()
    assert rendered[0] == "NAME  ROWS"
    assert rendered[2] == "a        5"  # text left-aligned, numbers right-aligned
    assert rendered[3] == "bbb   1234"


def test_ai_commands_and_retry_with_changes_parse() -> None:
    diagnose = parse("ai", "diagnose", "20260921-184645-ccbe5f", "--json")
    assert diagnose.run_id == "20260921-184645-ccbe5f" and diagnose.json
    draft = parse("ai", "draft", "mrr_v2", "--sample", "x.csv", "--from", "mrr_monthly", "-o", "o")
    assert (draft.name, draft.sample, draft.source, draft.output) == (
        "mrr_v2",
        "x.csv",
        "mrr_monthly",
        "o",
    )
    retry = parse("retry", "r1", "--set", "browser.timeout_ms=60000")
    assert retry.set == ["browser.timeout_ms=60000"]
