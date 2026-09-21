"""Load report configs from YAML with ``${ENV_VAR}`` interpolation.

Secrets are never written into YAML. A config references them as
``${METABASE_PASSWORD}`` (required) or ``${NAME:-default}`` (optional) and the
values are resolved from the process environment at load time. Every missing
variable and every validation problem is reported together, with its location,
so a broken config can be fixed in one pass.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from dashdashgo.config.models import ReportConfig
from dashdashgo.errors import ConfigurationError, ReportNotConfiguredError

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def interpolate_env(value: Any, env: Mapping[str, str], missing: set[str]) -> Any:
    """Recursively substitute ``${VAR}`` / ``${VAR:-default}`` in strings."""
    if isinstance(value, dict):
        return {k: interpolate_env(v, env, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate_env(v, env, missing) for v in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in env and env[name] != "":
            return env[name]
        if default is not None:
            return default
        missing.add(name)
        return ""

    return _ENV_REF.sub(replace, value)


def _format_validation_error(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        message = error["msg"].removeprefix("Value error, ")
        lines.append(f"  - {location}: {message}")
    return "\n".join(lines)


def load_report_config(path: Path, env: Mapping[str, str] | None = None) -> ReportConfig:
    env = os.environ if env is None else env
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigurationError(f"{path.name}: cannot read file: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"{path.name}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path.name}: expected a mapping at the top level")

    missing: set[str] = set()
    resolved = interpolate_env(raw, env, missing)
    if missing:
        raise ConfigurationError(
            f"{path.name}: environment variables not set: {', '.join(sorted(missing))}"
        )
    try:
        config = ReportConfig.model_validate(resolved)
    except ValidationError as exc:
        raise ConfigurationError(
            f"{path.name}: invalid configuration\n{_format_validation_error(exc)}"
        ) from None
    if config.name != path.stem:
        raise ConfigurationError(
            f"{path.name}: 'name' is {config.name!r} but must match the file name {path.stem!r}"
        )
    return config


class ReportRegistry:
    """Discovers report configs in a directory.

    Configs are re-read on every ``load`` so edits take effect on the next run
    without restarting the service.
    """

    def __init__(self, reports_dir: Path, env: Mapping[str, str] | None = None) -> None:
        self.reports_dir = reports_dir
        self._env = env

    def names(self) -> list[str]:
        if not self.reports_dir.is_dir():
            return []
        return sorted(p.stem for p in self.reports_dir.glob("*.yaml"))

    def path_for(self, name: str) -> Path:
        path = self.reports_dir / f"{name}.yaml"
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,62}", name) or not path.is_file():
            raise ReportNotConfiguredError(
                f"no report named {name!r}; available: {', '.join(self.names()) or 'none'}"
            )
        return path

    def load(self, name: str) -> ReportConfig:
        return load_report_config(self.path_for(name), self._env)

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
