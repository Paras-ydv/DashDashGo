"""Load report configs from YAML with ``${ENV_VAR}`` interpolation.

Secrets are never written into YAML. A config references them as
``${METABASE_PASSWORD}`` (required) or ``${NAME:-default}`` (optional) and the
values are resolved from the process environment at load time. Every missing
variable and every validation problem is reported together, with its location,
so a broken config can be fixed in one pass.

The same functions serve every entry point: files on disk (registry), text
from the UI editor (config store) and ``--set`` overrides from the CLI.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from dashdashgo.config.models import ReportConfig
from dashdashgo.config.policy import ConfigPolicy, hostname
from dashdashgo.errors import ConfigurationError, ReportNotConfiguredError

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
REPORT_NAME = re.compile(r"[a-z][a-z0-9_]{1,62}")


def interpolate_env(
    value: Any,
    env: Mapping[str, str],
    missing: set[str],
    referenced: set[str] | None = None,
) -> Any:
    """Recursively substitute ``${VAR}`` / ``${VAR:-default}`` in strings.

    Unset variables are added to ``missing``; every referenced name is added to
    ``referenced`` (when given) so a policy can vet them.
    """
    if isinstance(value, dict):
        return {k: interpolate_env(v, env, missing, referenced) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate_env(v, env, missing, referenced) for v in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if referenced is not None:
            referenced.add(name)
        if name in env and env[name] != "":
            return env[name]
        if default is not None:
            return default
        missing.add(name)
        return ""

    return _ENV_REF.sub(replace, value)


def is_env_reference(value: Any) -> bool:
    """True if the whole value is a single ``${VAR}`` reference."""
    return isinstance(value, str) and _ENV_REF.fullmatch(value.strip()) is not None


def parse_yaml(text: str, source: str) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"line {mark.line + 1}" if mark is not None else "(root)"
        detail = getattr(exc, "problem", None) or str(exc)
        raise ConfigurationError(
            f"{source}: invalid YAML at {where}: {detail}", problems=[(where, str(detail))]
        ) from exc
    if not isinstance(raw, dict):
        raise ConfigurationError(
            f"{source}: expected a mapping at the top level",
            problems=[("(root)", "expected a mapping")],
        )
    return raw


def apply_overrides(raw: dict[str, Any], overrides: Sequence[str]) -> dict[str, Any]:
    """Apply ``dotted.path=value`` overrides (values parsed as YAML) to a raw config.

    ``browser.headless=false``, ``retry.max_attempts=1`` and
    ``source.filters.usage_date=past30days`` all work; intermediate mappings are
    created as needed. Overrides are applied before validation, so they are
    checked exactly like file contents.
    """
    result: dict[str, Any] = yaml.safe_load(yaml.safe_dump(raw))  # deep copy
    for item in overrides:
        path, sep, value_text = item.partition("=")
        keys = [k for k in path.strip().split(".") if k]
        if not sep or not keys:
            raise ConfigurationError(
                f"invalid override {item!r}; expected key.path=value",
                problems=[(item, "expected key.path=value")],
            )
        node = result
        for key in keys[:-1]:
            child = node.setdefault(key, {})
            if not isinstance(child, dict):
                raise ConfigurationError(
                    f"override {item!r}: '{key}' is not a mapping",
                    problems=[(path, f"'{key}' is not a mapping")],
                )
            node = child
        node[keys[-1]] = yaml.safe_load(value_text) if value_text.strip() else ""
    return result


def validate_report(
    raw: dict[str, Any],
    *,
    source: str,
    expected_name: str | None = None,
    env: Mapping[str, str] | None = None,
) -> ReportConfig:
    env = os.environ if env is None else env
    policy = ConfigPolicy.from_env(env)
    missing: set[str] = set()
    referenced: set[str] = set()
    resolved = interpolate_env(raw, env, missing, referenced)
    if denied := policy.denied_env(referenced):
        allowed = ", ".join(policy.env_allowlist)
        raise ConfigurationError(
            f"{source}: environment variables not allowed in configs: {', '.join(denied)} "
            f"(allowed: {allowed}; see CONFIG_ENV_ALLOWLIST)",
            problems=[
                ("(environment)", f"{n} may not be referenced (allowed: {allowed})") for n in denied
            ],
        )
    if missing:
        names = ", ".join(sorted(missing))
        raise ConfigurationError(
            f"{source}: environment variables not set: {names}",
            problems=[
                ("(environment)", f"environment variable {n} is not set") for n in sorted(missing)
            ],
        )
    try:
        config = ReportConfig.model_validate(resolved)
    except ValidationError as exc:
        problems = [
            (
                ".".join(str(part) for part in error["loc"]) or "(root)",
                error["msg"].removeprefix("Value error, "),
            )
            for error in exc.errors()
        ]
        details = "\n".join(f"  - {loc}: {msg}" for loc, msg in problems)
        raise ConfigurationError(
            f"{source}: invalid configuration\n{details}", problems=problems
        ) from None
    if not policy.host_allowed(config.source.base_url):
        message = (
            f"dashboard host {hostname(config.source.base_url)!r} is not allowed "
            f"(allowed: {', '.join(policy.allowed_hosts)}; see ALLOWED_DASHBOARD_HOSTS)"
        )
        raise ConfigurationError(f"{source}: {message}", problems=[("source.base_url", message)])
    if expected_name is not None and config.name != expected_name:
        message = f"'name' is {config.name!r} but must match the file name {expected_name!r}"
        raise ConfigurationError(f"{source}: {message}", problems=[("name", message)])
    return config


def load_report_config(
    path: Path, env: Mapping[str, str] | None = None, overrides: Sequence[str] = ()
) -> ReportConfig:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"{path.name}: cannot read file: {exc}") from exc
    raw = parse_yaml(text, path.name)
    if overrides:
        raw = apply_overrides(raw, overrides)
    return validate_report(raw, source=path.name, expected_name=path.stem, env=env)


class ReportRegistry:
    """Discovers report configs in a directory.

    Configs are re-read on every ``load`` so edits (from the UI, the CLI or a
    text editor) take effect on the next run without restarting the service.
    """

    def __init__(self, reports_dir: Path, env: Mapping[str, str] | None = None) -> None:
        self.reports_dir = reports_dir
        self.env = env

    def names(self) -> list[str]:
        if not self.reports_dir.is_dir():
            return []
        return sorted(p.stem for p in self.reports_dir.glob("*.yaml"))

    def path_for(self, name: str) -> Path:
        path = self.reports_dir / f"{name}.yaml"
        if not REPORT_NAME.fullmatch(name) or not path.is_file():
            raise ReportNotConfiguredError(
                f"no report named {name!r}; available: {', '.join(self.names()) or 'none'}"
            )
        return path

    def load(self, name: str, overrides: Sequence[str] = ()) -> ReportConfig:
        return load_report_config(self.path_for(name), self.env, overrides)

    def load_all(self) -> tuple[dict[str, ReportConfig], dict[str, ConfigurationError]]:
        """Load every config; invalid ones are returned separately instead of raising."""
        valid: dict[str, ReportConfig] = {}
        invalid: dict[str, ConfigurationError] = {}
        for name in self.names():
            try:
                valid[name] = self.load(name)
            except ConfigurationError as exc:
                invalid[name] = exc
        return valid, invalid
