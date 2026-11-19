"""The one way application code writes an operational event."""

import logging
from typing import Any


def event(
    logger: logging.Logger,
    name: str,
    *,
    level: int = logging.INFO,
    exc_info: bool = False,
    **fields: Any,
) -> None:
    """Record ``name`` (``<category>.<what_happened>``) with identifiers, counts and codes.

    Pass stable ids, durations, counts and fixed codes only; never names, emails, tokens or any
    text a person wrote or said. With ``exc_info`` the exception *class and location* are logged,
    not its message. This function never raises.
    """
    try:
        if logger.isEnabledFor(level):
            logger.log(level, name, exc_info=exc_info, extra={"event": name, "fields": fields})
    except Exception:  # logging must never affect the caller
        pass
