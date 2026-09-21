"""ClickHouse connection management.

The client is created lazily and shared: the API must be able to start (and
report itself degraded) while ClickHouse is still booting, and pipelines get a
retried connection attempt instead of assuming "container started" means
"service ready".
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

import clickhouse_connect
from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import (
    ClickHouseError,
    DatabaseError,
    OperationalError,
)

from dashdashgo.errors import LoadError, WarehouseConnectionError, WarehouseError
from dashdashgo.settings import Settings

log = logging.getLogger(__name__)

T = TypeVar("T")


class ClickHouse:
    """Thin wrapper translating driver errors into DashDashGo's error types."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Client | None = None
        self._lock = threading.Lock()

    def client(self) -> Client:
        with self._lock:
            if self._client is None:
                s = self._settings
                try:
                    self._client = clickhouse_connect.get_client(
                        host=s.clickhouse_host,
                        port=s.clickhouse_port,
                        username=s.clickhouse_user,
                        password=s.clickhouse_password.get_secret_value(),
                        secure=s.clickhouse_secure,
                        connect_timeout=s.clickhouse_connect_timeout_s,
                        send_receive_timeout=s.clickhouse_query_timeout_s,
                        # No server-side session: the client is shared across threads.
                        autogenerate_session_id=False,
                    )
                except (OperationalError, OSError) as exc:
                    raise WarehouseConnectionError(
                        f"cannot connect to ClickHouse at {s.clickhouse_host}:{s.clickhouse_port}: "
                        f"{_first_line(exc)}"
                    ) from exc
                except DatabaseError as exc:
                    raise WarehouseError(
                        f"ClickHouse rejected the connection: {_first_line(exc)}"
                    ) from exc
            return self._client

    def _run(self, action: Callable[[Client], T]) -> T:
        try:
            return action(self.client())
        except OperationalError as exc:
            raise WarehouseConnectionError(f"ClickHouse unavailable: {_first_line(exc)}") from exc
        except ClickHouseError as exc:
            raise WarehouseError(f"ClickHouse error: {_first_line(exc)}") from exc

    def command(self, sql: str, parameters: dict[str, Any] | None = None) -> Any:
        return self._run(lambda c: c.command(sql, parameters=parameters))

    def query_rows(
        self, sql: str, parameters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        def run(c: Client) -> list[dict[str, Any]]:
            result = c.query(sql, parameters=parameters)
            return [dict(zip(result.column_names, row, strict=True)) for row in result.result_rows]

        return self._run(run)

    def insert_columns(
        self,
        table: str,
        columns: Sequence[Sequence[Any]],
        column_names: list[str],
        settings: dict[str, Any] | None = None,
    ) -> None:
        def run(c: Client) -> None:
            c.insert(
                table,
                columns,
                column_names=column_names,
                column_oriented=True,
                settings=settings or {},
            )

        try:
            self._run(run)
        except WarehouseConnectionError:
            raise
        except WarehouseError as exc:
            # Server-side insert failures (memory, too many parts, ...) are usually
            # transient; type errors are caught long before this point by coercion.
            raise LoadError(exc.message) from exc

    def ping(self) -> bool:
        try:
            return bool(self.client().ping())
        except WarehouseError:
            return False


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__
