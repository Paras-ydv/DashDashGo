"""Dashboard adapters, keyed by the ``source.platform`` config value."""

from __future__ import annotations

from dashdashgo.acquisition.adapters.base import DashboardAdapter
from dashdashgo.acquisition.adapters.metabase import MetabaseAdapter
from dashdashgo.config.models import ReportConfig
from dashdashgo.errors import ConfigurationError

ADAPTERS: dict[str, type[MetabaseAdapter]] = {"metabase": MetabaseAdapter}


def create_adapter(report: ReportConfig) -> DashboardAdapter:
    adapter_cls = ADAPTERS.get(report.source.platform)
    if adapter_cls is None:
        raise ConfigurationError(f"no adapter for platform {report.source.platform!r}")
    return adapter_cls(report.source, report.browser)


__all__ = ["ADAPTERS", "DashboardAdapter", "MetabaseAdapter", "create_adapter"]
