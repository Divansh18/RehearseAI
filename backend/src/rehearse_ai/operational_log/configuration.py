"""Configure operational logging once, replace it cleanly, shut it down cleanly."""

import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from rehearse_ai.config import LogSettings, repository_root
from rehearse_ai.operational_log.events import event
from rehearse_ai.operational_log.formatter import JsonFormatter
from rehearse_ai.operational_log.handlers import (
    BoundedQueueHandler,
    BoundedQueueListener,
    PrunedRotatingFileHandler,
)

LOGGER_NAME = "rehearse_ai"
INTERVIEW_PREFIXES = ("rehearse_ai.interview", "rehearse_ai.voice")


class _Route(logging.Filter):
    """Send interview and voice events to one file and everything else to the other."""

    def __init__(self, interview: bool) -> None:
        super().__init__()
        self.interview = interview

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith(INTERVIEW_PREFIXES) == self.interview


def resolve_directory(directory: Path) -> Path:
    """An absolute log directory, the same whether started from the repository root or backend/."""
    if directory.is_absolute():
        return directory
    return ((repository_root() or Path.cwd()) / directory).resolve()


@dataclass
class LoggingHandle:
    directory: Path | None
    handler: BoundedQueueHandler
    listener: BoundedQueueListener
    file_handlers: list[PrunedRotatingFileHandler]

    def stats(self) -> dict[str, int]:
        return {
            "dropped": sum(self.handler.dropped.values()),
            "handler_failures": self.handler.failures,
        }

    def shutdown(self, timeout: float = 5.0) -> None:
        """Flush the queue, stop the writer thread and close every file. Safe to call twice."""
        global _active
        logger = logging.getLogger(LOGGER_NAME)
        logger.removeHandler(self.handler)
        self.listener.stop(timeout)
        for handler in [*self.file_handlers, self.handler]:
            try:
                handler.close()
            except Exception:  # a failing disk must not make shutdown fail
                pass
        if _active is self:
            _active = None


_active: LoggingHandle | None = None


def configure_logging(settings: LogSettings, *, build_id: str | None = None) -> LoggingHandle:
    global _active
    """Attach structured logging to the ``rehearse_ai`` logger.

    Calling it again replaces the previous configuration, so handlers and events are never
    duplicated. A log directory that cannot be created disables the files, not the application.
    """
    if _active is not None:
        _active.shutdown()
    formatter = JsonFormatter(build_id)
    level = getattr(logging, settings.level)
    writers: list[logging.Handler] = []
    file_handlers: list[PrunedRotatingFileHandler] = []
    directory: Path | None = None
    if settings.files:
        directory = resolve_directory(settings.directory)
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            for filename, interview in (("application.jsonl", False), ("interview.jsonl", True)):
                handler = PrunedRotatingFileHandler(
                    directory / filename,
                    max_bytes=settings.max_bytes,
                    backups=settings.backups,
                    retention_days=settings.retention_days,
                )
                handler.addFilter(_Route(interview))
                file_handlers.append(handler)
        except OSError as exc:
            print(
                f"rehearse_ai: log files disabled, cannot use {directory} ({type(exc).__name__})",
                file=sys.stderr,
            )
            directory = None
        writers.extend(file_handlers)
    if settings.console:
        writers.append(logging.StreamHandler(sys.stdout))
    for writer in writers:
        writer.setFormatter(formatter)
        writer.setLevel(level)
    handler = BoundedQueueHandler(settings.queue_size)
    listener = BoundedQueueListener(handler.queue, *writers, respect_handler_level=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False  # the root logger must not print every event a second time
    logger.addHandler(handler)
    listener.start()
    _active = LoggingHandle(directory, handler, listener, file_handlers)
    event(
        logger,
        "log.configured",
        directory=str(directory) if directory else None,
        console=settings.console,
        log_level=settings.level,
        max_bytes=settings.max_bytes,
        backups=settings.backups,
        retention_days=settings.retention_days,
        queue_size=settings.queue_size,
    )
    return _active
