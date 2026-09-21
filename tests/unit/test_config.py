from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from dashdashgo.config.loader import ReportRegistry, interpolate_env, load_report_config
from dashdashgo.config.models import ReportConfig, ReportFormat
from dashdashgo.errors import ConfigurationError, ReportNotConfiguredError
from tests.conftest import REPORTS_DIR, TEST_ENV

MakeConfig = Callable[[dict[str, Any]], ReportConfig]


def test_interpolation_substitutes_and_applies_defaults() -> None:
    missing: set[str] = set()
    result = interpolate_env(
        {"a": "${X}", "b": ["${Y:-fallback}"], "c": 3, "d": "pre-${X}-post"}, {"X": "1"}, missing
    )
    assert result == {"a": "1", "b": ["fallback"], "c": 3, "d": "pre-1-post"}
    assert missing == set()


def test_interpolation_reports_missing_variables() -> None:
    missing: set[str] = set()
    interpolate_env({"a": "${NOPE}", "b": "${ALSO_NOPE}"}, {}, missing)
    assert missing == {"NOPE", "ALSO_NOPE"}


def test_valid_config_loads_with_secrets_masked(sample_config: ReportConfig) -> None:
    assert sample_config.source.base_url == "http://metabase.test:3000"
    assert sample_config.source.export.format is ReportFormat.CSV
    assert sample_config.source.credentials.password.get_secret_value() == "s3cret-Passw0rd"
    assert "s3cret" not in repr(sample_config)
    assert "s3cret" not in sample_config.model_dump_json()
    assert sample_config.unique_key == ["report_date", "region"]


def test_missing_env_var_fails_before_validation(
    config_dict: dict[str, Any], write_config: Callable[[dict[str, Any]], Path]
) -> None:
    env = {k: v for k, v in TEST_ENV.items() if k != "METABASE_PASSWORD"}
    with pytest.raises(ConfigurationError, match="METABASE_PASSWORD"):
        load_report_config(write_config(config_dict), env)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda c: c["source"].pop("base_url"), "source.base_url: Field required"),
        (lambda c: c["source"].update(base_url="not-a-url"), "base_url"),
        (lambda c: c["source"]["export"].update(format="pdf"), "source.export.format"),
        (lambda c: c.update(unexpected=1), "unexpected: Extra inputs are not permitted"),
        (lambda c: c["retry"].update(max_attempts=0), "retry.max_attempts"),
        (lambda c: c.update(browser={"timeout_ms": 5}), "browser.timeout_ms"),
        (lambda c: c.update(schedule={"enabled": True}), "'cron' is required"),
        (lambda c: c.update(schedule={"enabled": True, "cron": "every day"}), "invalid cron"),
        (
            lambda c: c.update(schedule={"cron": "0 8 * * 1", "timezone": "Mars/Base"}),
            "unknown timezone",
        ),
        (
            lambda c: c["destination"]["columns"][2].update(type="Array(String)"),
            "unsupported column type",
        ),
        (lambda c: c["destination"].update(order_by=["nope"]), "order_by references unknown"),
        (lambda c: c["destination"].pop("table"), "destination.table: Field required"),
        (lambda c: c["source"]["location"].pop("card"), "'card' is required"),
        (lambda c: c["source"]["location"].update(question="Q"), "exactly one of"),
        (lambda c: c["ingestion"]["transforms"].append("explode"), "unknown transform 'explode'"),
        (
            lambda c: c["ingestion"]["transforms"].append({"rename": {"cols": {}}}),
            "invalid options for transform 'rename'",
        ),
        (
            lambda c: c["ingestion"]["quality"]["rules"].append({"column": "ghost", "min": 1}),
            "unknown destination columns",
        ),
        (lambda c: c["source"]["credentials"].update(password=""), "password"),
    ],
)
def test_invalid_configs_fail_with_located_message(
    config_dict: dict[str, Any],
    make_config: MakeConfig,
    mutate: Callable[[dict[str, Any]], Any],
    message: str,
) -> None:
    mutate(config_dict)
    with pytest.raises(ConfigurationError) as info:
        make_config(config_dict)
    assert message in info.value.message


def test_name_must_match_file_name(config_dict: dict[str, Any], tmp_path: Path) -> None:
    import yaml

    path = tmp_path / "other_name.yaml"
    path.write_text(yaml.safe_dump(config_dict))
    with pytest.raises(ConfigurationError, match="must match the file name"):
        load_report_config(path, TEST_ENV)


def test_invalid_yaml_is_a_configuration_error(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("name: [unclosed")
    with pytest.raises(ConfigurationError, match="invalid YAML"):
        load_report_config(path, TEST_ENV)


@pytest.mark.parametrize("name", ["weekly_sales", "customer_usage", "q4_budget_review"])
def test_shipped_report_configs_are_valid(name: str) -> None:
    config = ReportRegistry(REPORTS_DIR, TEST_ENV).load(name)
    assert config.name == name


def test_registry_rejects_unknown_and_path_like_names() -> None:
    registry = ReportRegistry(REPORTS_DIR, TEST_ENV)
    with pytest.raises(ReportNotConfiguredError):
        registry.load("does_not_exist")
    with pytest.raises(ReportNotConfiguredError):
        registry.load("../etc/passwd")


def test_registry_load_all_separates_invalid(
    config_dict: dict[str, Any], write_config: Callable[[dict[str, Any]], Path]
) -> None:
    path = write_config(config_dict)
    broken = dict(config_dict, name="broken_report")
    broken["retry"] = {"max_attempts": 99}
    write_config(broken)
    valid, invalid = ReportRegistry(path.parent, TEST_ENV).load_all()
    assert list(valid) == ["sample_report"]
    assert list(invalid) == ["broken_report"]
