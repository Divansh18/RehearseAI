"""Correlation identifiers carried through a request, a job or a voice session.

Only stable identifiers belong here (random UUIDs and kinds), never names, addresses or text. The
values live in ``contextvars``, so every log record written while they are bound carries them,
including records from tasks created inside the bound scope.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

CORRELATION_KEYS = ("request_id", "assignment_id", "record_id", "job_id")

_bound: ContextVar[dict[str, str]] = ContextVar("rehearse_log_context", default={})


def current() -> dict[str, str]:
    return dict(_bound.get())


def bind(**identifiers: object) -> None:
    """Add identifiers to the current context (for a task that owns its own context)."""
    merged = dict(_bound.get())
    for key, value in identifiers.items():
        if key in CORRELATION_KEYS and value is not None:
            merged[key] = str(value)
    _bound.set(merged)


@contextmanager
def bound(**identifiers: object) -> Iterator[None]:
    """Bind identifiers for the duration of a block, then restore the previous context."""
    token = _bound.set(_bound.get())
    try:
        bind(**identifiers)
        yield
    finally:
        _bound.reset(token)
