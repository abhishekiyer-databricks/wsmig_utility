"""
HTTP retry / backoff helper.

Adapts the customer inventory script's retry behaviour: retry on HTTP 429 and 5xx with
exponential backoff, honouring a `Retry-After` header when present, capped per attempt.
"""
from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class RetryableHTTPError(Exception):
    """Raised internally by callers to signal a retryable status to `with_retry`."""

    def __init__(self, status: int, message: str = "", retry_after: float | None = None) -> None:
        super().__init__(message or f"HTTP {status}")
        self.status = status
        self.retry_after = retry_after


def is_retryable_status(status: int) -> bool:
    return status in _RETRYABLE_STATUS


def with_retry(
    fn: Callable[[], T],
    *,
    max_attempts: int = 5,
    backoff: float = 1.0,
    max_sleep: float = 30.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> T:
    """Call `fn()` with retry on RetryableHTTPError.

    Exponential backoff: sleep = min(backoff * 2**attempt, max_sleep), or the server's
    Retry-After if larger. Re-raises the last error after `max_attempts` tries. Non-retryable
    exceptions propagate immediately.
    """
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except RetryableHTTPError as exc:
            last_exc = exc
            if attempt == max_attempts - 1:
                break
            wait = min(backoff * (2 ** attempt), max_sleep)
            if exc.retry_after is not None:
                wait = max(wait, exc.retry_after)
            sleep_fn(wait)
    assert last_exc is not None
    raise last_exc
