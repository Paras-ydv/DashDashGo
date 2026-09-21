"""Config editing: --set overrides and the versioned config store."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from dashdashgo.config.loader import ReportRegistry, apply_overrides
from dashdashgo.config.store import BLANK_TEMPLATE, ConfigStore
from dashdashgo.errors import ConfigConflictError, ConfigurationError
from tests.conftest import REPORTS_DIR, TEST_ENV


@pytest.fixture
def store(tmp_path: Path) -> ConfigStore:
    directory = tmp_path / "reports"
    shutil.copytree(REPORTS_DIR, directory)
    return ConfigStore(ReportRegistry(directory, TEST_ENV))


# --- overrides --------------------------------------------------------------------


def test_overrides_set_typed_values_and_create_paths() -> None:
    raw = {"browser": {"headless": True}, "retry": {"max_attempts": 3}}
    result = apply_overrides(
        raw,
        ["browser.headless=false", "retry.max_attempts=1", "source.filters.usage_date=past30days"],
    )
    assert result["browser"]["headless"] is False
    assert result["retry"]["max_attempts"] == 1
    assert result["source"]["filters"] == {"usage_date": "past30days"}
    assert raw["browser"]["headless"] is True  # input untouched


@pytest.mark.parametrize("bad", ["no_equals_sign", "=value", "retry.max_attempts.x=1"])
def test_invalid_overrides_are_configuration_errors(bad: str) -> None:
    with pytest.raises(ConfigurationError):
        apply_overrides({"retry": {"max_attempts": 3}}, [bad])


def test_registry_load_applies_and_validates_overrides() -> None:
    registry = ReportRegistry(REPORTS_DIR, TEST_ENV)
    config = registry.load("weekly_sales", ["browser.headless=false", "retry.max_attempts=1"])
    assert config.browser.headless is False and config.retry.max_attempts == 1
    with pytest.raises(ConfigurationError, match=r"retry\.max_attempts"):
        registry.load("weekly_sales", ["retry.max_attempts=0"])


# --- store ------------------------------------------------------------------------


def test_save_is_validated_versioned_and_keeps_history(store: ConfigStore) -> None:
    doc = store.read("weekly_sales")
    edited = doc.text.replace("max_attempts: 3", "max_attempts: 5")
    saved = store.save("weekly_sales", edited, doc.version)
    assert saved.version != doc.version
    assert store.read("weekly_sales").text == edited
    [previous] = store.history("weekly_sales")
    assert store.read_version("weekly_sales", previous.id) == doc.text


def test_stale_version_is_rejected(store: ConfigStore) -> None:
    doc = store.read("weekly_sales")
    store.save("weekly_sales", doc.text.replace("max_attempts: 3", "max_attempts: 4"), doc.version)
    with pytest.raises(ConfigConflictError, match="changed since it was opened"):
        store.save("weekly_sales", doc.text, doc.version)


def test_invalid_edit_is_not_written(store: ConfigStore) -> None:
    doc = store.read("weekly_sales")
    broken = doc.text.replace("max_attempts: 3", "max_attempts: 0")
    with pytest.raises(ConfigurationError) as info:
        store.save("weekly_sales", broken, doc.version)
    assert "retry.max_attempts" in dict(info.value.problems)
    assert store.read("weekly_sales").text == doc.text
    assert store.history("weekly_sales") == []


def test_plaintext_secrets_are_rejected_with_other_problems(store: ConfigStore) -> None:
    text = store.read("weekly_sales").text.replace("${METABASE_PASSWORD}", "hunter2")
    text = text.replace("max_attempts: 3", "max_attempts: 0")
    with pytest.raises(ConfigurationError) as info:
        store.check("weekly_sales", text)
    locations = [loc for loc, _ in info.value.problems]
    assert locations[0] == "source.credentials.password"
    assert "retry.max_attempts" in locations


def test_yaml_syntax_errors_report_the_line(store: ConfigStore) -> None:
    with pytest.raises(ConfigurationError) as info:
        store.check("weekly_sales", "name: weekly_sales\nsource: [unclosed\n")
    assert info.value.problems[0][0].startswith("line ")


def test_create_from_template_and_blank(store: ConfigStore) -> None:
    copy = store.template("sales_copy", source="weekly_sales")
    assert copy.splitlines()[3] == "name: sales_copy"
    assert store.create("sales_copy", copy).name == "sales_copy"
    store.check("fresh_report", BLANK_TEMPLATE.format(name="fresh_report"))
    with pytest.raises(ConfigConflictError):
        store.create("sales_copy", copy)
    with pytest.raises(ConfigurationError, match="reserved"):
        store.create("new", store.template("new"))
    with pytest.raises(ConfigurationError, match="invalid report name"):
        store.create("Bad-Name", copy)


def test_archive_keeps_the_file(store: ConfigStore, tmp_path: Path) -> None:
    archived = store.archive("q4_budget_review")
    assert archived.startswith(".archive/q4_budget_review-")
    assert "q4_budget_review" not in ReportRegistry(tmp_path / "reports", TEST_ENV).names()
    assert (tmp_path / "reports" / archived).is_file()


def test_history_rejects_path_tricks(store: ConfigStore) -> None:
    with pytest.raises(ConfigurationError):
        store.read_version("weekly_sales", "../../weekly_sales")
