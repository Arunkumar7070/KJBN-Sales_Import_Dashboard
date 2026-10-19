"""The application's notion of "now", which tests and evaluators can move.

utc_now() is the real time unless a frozen time is active in the current
context (one request, or one background task). The X-Test-Clock request header
freezes it for that request when the ALLOW_TEST_CLOCK setting is on.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

TEST_CLOCK_HEADER = "X-Test-Clock"

_frozen_now: ContextVar[datetime | None] = ContextVar("frozen_now", default=None)


class InvalidTestClock(ValueError):
    pass


def system_now() -> datetime:
    return datetime.now(UTC)


def utc_now() -> datetime:
    return _frozen_now.get() or system_now()


def frozen_now() -> datetime | None:
    """The frozen time if one is active here, else None (the real clock is used)."""
    return _frozen_now.get()


@contextmanager
def frozen_at(moment: datetime | None) -> Iterator[None]:
    """Make utc_now() return moment inside this block; None leaves the real clock."""
    token = _frozen_now.set(moment)
    try:
        yield
    finally:
        _frozen_now.reset(token)


def parse_test_clock(value: str) -> datetime:
    """Parse an ISO-8601 date-time as UTC; a value without an offset is taken as UTC."""
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError as error:
        raise InvalidTestClock(
            f"Invalid {TEST_CLOCK_HEADER} header: expected an ISO-8601 date-time "
            "such as 2026-10-17T09:00:00Z."
        ) from error
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)
