"""Guard rails for configs that come from the UI, the API or a ``--set`` override.

A report config can be edited by anyone who can reach the UI, so it must not be
able to:

* read arbitrary environment variables - ``${CLICKHOUSE_PASSWORD}`` in a filter
  value would end up in a URL, a log line or the dashboard's access log;
* point the browser (with the dashboard credentials) at any host - the login
  form would post them to an attacker's server;
* pass Chromium flags that redirect or expose the browser (proxies, custom DNS,
  remote debugging).

The policy is read from the same environment the config is interpolated from,
so tests and the CLI can pass an explicit mapping.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_ENV_ALLOWLIST = ("METABASE_*", "DASHBOARD_*", "REPORT_*")
# Never interpolated, whatever the allow-list says: infrastructure and API secrets.
_ALWAYS_DENIED = ("CLICKHOUSE_*", "POSTGRES_*", "DEMO_READER_*", "AI_*", "AUTH_*")

BLOCKED_LAUNCH_ARGS = (
    "--proxy-server",
    "--proxy-pac-url",
    "--proxy-bypass-list",
    "--host-resolver-rules",
    "--host-rules",
    "--remote-debugging-port",
    "--remote-debugging-address",
    "--remote-debugging-pipe",
    "--remote-allow-origins",
    "--user-data-dir",
    "--disable-web-security",
    "--load-extension",
    "--utility-cmd-prefix",
    "--renderer-cmd-prefix",
    "--gpu-launcher",
    "--browser-subprocess-path",
)


def blocked_launch_arg(arg: str) -> str | None:
    """The blocked flag ``arg`` sets, if any (``--proxy-server=x`` -> ``--proxy-server``)."""
    flag = arg.strip().split("=", 1)[0].lower()
    return flag if flag in BLOCKED_LAUNCH_ARGS else None


def _split(value: str | None) -> tuple[str, ...]:
    return tuple(p.strip() for p in (value or "").split(",") if p.strip())


def hostname(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


@dataclass(frozen=True)
class ConfigPolicy:
    env_allowlist: tuple[str, ...] = DEFAULT_ENV_ALLOWLIST
    allowed_hosts: tuple[str, ...] = ()
    """Dashboard hostnames a config may target; empty = any (nothing to derive from)."""

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ConfigPolicy:
        """``CONFIG_ENV_ALLOWLIST`` and ``ALLOWED_DASHBOARD_HOSTS`` (comma lists).

        Hosts default to the host of ``METABASE_URL``; ``*`` allows any host.
        """
        allowlist = _split(env.get("CONFIG_ENV_ALLOWLIST")) or DEFAULT_ENV_ALLOWLIST
        hosts = _split(env.get("ALLOWED_DASHBOARD_HOSTS"))
        if not hosts and env.get("METABASE_URL"):
            hosts = (hostname(env["METABASE_URL"]),)
        if "*" in hosts:
            hosts = ()
        return cls(allowlist, tuple(h.lower() for h in hosts if h))

    def env_allowed(self, name: str) -> bool:
        if any(fnmatch.fnmatchcase(name, p) for p in _ALWAYS_DENIED):
            return False
        return any(fnmatch.fnmatchcase(name, p) for p in self.env_allowlist)

    def denied_env(self, names: Iterable[str]) -> list[str]:
        return sorted({n for n in names if not self.env_allowed(n)})

    def host_allowed(self, url: str) -> bool:
        return not self.allowed_hosts or hostname(url) in self.allowed_hosts
