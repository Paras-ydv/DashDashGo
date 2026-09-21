"""Retry with exponential backoff - only for failures that can plausibly heal."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError

from dashdashgo.config.models import RetryConfig
from dashdashgo.errors import DashDashGoError

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_delay: float = 2.0
    multiplier: float = 2.0
    max_delay: float = 60.0

    @classmethod
    def from_config(cls, config: RetryConfig) -> RetryPolicy:
        return cls(
            max_attempts=config.max_attempts,
            initial_delay=config.initial_delay_seconds,
            multiplier=config.backoff_multiplier,
            max_delay=config.max_delay_seconds,
        )

    def delay_after(self, attempt: int) -> float:
        """Seconds to wait after failed attempt number ``attempt`` (1-based)."""
        return float(min(self.initial_delay * self.multiplier ** (attempt - 1), self.max_delay))


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, DashDashGoError):
        return exc.retryable
    # Raw Playwright errors are timeouts, crashed targets and network failures.
    return isinstance(exc, PlaywrightError | ConnectionError | TimeoutError)


RetryCallback = Callable[[int, BaseException, float], None]


def call_with_retry[T](
    operation: Callable[[int], T],
    policy: RetryPolicy,
    *,
    description: str,
    on_retry: RetryCallback | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``operation(attempt)`` until it succeeds, fails permanently or attempts run out.

    Non-retryable errors propagate immediately: retrying a wrong password or a
    malformed file only wastes time (and can lock accounts).
    """
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return operation(attempt)
        except Exception as exc:
            if not is_retryable(exc) or attempt == policy.max_attempts:
                raise
            delay = policy.delay_after(attempt)
            log.warning(
                "%s failed on attempt %d/%d (%s: %s); retrying in %.1fs",
                description,
                attempt,
                policy.max_attempts,
                type(exc).__name__,
                str(exc).splitlines()[0] if str(exc) else "",
                delay,
            )
            if on_retry:
                on_retry(attempt, exc, delay)
            sleep(delay)
    raise AssertionError("unreachable: loop always returns or raises")
