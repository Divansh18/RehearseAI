"""Structured operational logging: what the system did and how long it took, never what was said.

This is separate from the protected per-attempt diagnostic files (``interview.session_log``,
which hold the conversation) and from the database audit trail (``AssignmentAudit``). See
docs/architecture/LOGGING.md.
"""

import logging

from rehearse_ai.operational_log.configuration import LoggingHandle, configure_logging
from rehearse_ai.operational_log.context import bind, bound
from rehearse_ai.operational_log.events import event

__all__ = ["LoggingHandle", "bind", "bound", "configure_logging", "event"]

# A library must not print through the "last resort" handler when logging is not configured.
logging.getLogger("rehearse_ai").addHandler(logging.NullHandler())
