"""Handlers that keep log I/O off the interview's hot path and bound disk use.

``BoundedQueueHandler`` is the only handler attached to the application logger. Calling code does
a non-blocking ``put_nowait`` onto a bounded queue; a background thread formats the records and
writes them (rotating files and the console). Nothing a log call does can wait for the disk.
"""

import copy
import logging
import logging.handlers
import os
import queue
import sys
import time
from collections import Counter
from pathlib import Path

from rehearse_ai.operational_log import context
from rehearse_ai.operational_log.formatter import safe_error


class BoundedQueueHandler(logging.handlers.QueueHandler):
    """Never blocks and never raises.

    Overflow policy: when the queue is full the *new* record is dropped and counted. As soon as a
    record fits again it is followed by one ``log.records_dropped`` event giving the count by level,
    so a gap in the log is always visible and never silent.
    """

    def __init__(self, maxsize: int) -> None:
        super().__init__(queue.Queue(maxsize))
        self.dropped: Counter[str] = Counter()
        self.failures = 0

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        # Capture what only exists on the calling thread (correlation ids, the traceback) now;
        # everything else is formatted later by the writer thread.
        prepared = copy.copy(record)
        prepared.correlation = context.current()
        if record.exc_info and record.exc_info[0] is not None:
            prepared.error = safe_error(record.exc_info)
        prepared.exc_info = None
        prepared.exc_text = None
        prepared.args = None
        return prepared

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped[record.levelname] += 1
            return
        if self.dropped:
            self._report_drops()

    def _report_drops(self) -> None:
        summary = logging.LogRecord(
            "rehearse_ai.operational_log",
            logging.WARNING,
            __file__,
            0,
            "log.records_dropped",
            (),
            None,
        )
        summary.event = "log.records_dropped"
        summary.fields = {"dropped": sum(self.dropped.values()), "by_level": dict(self.dropped)}
        summary.correlation = {}
        try:
            self.queue.put_nowait(summary)
        except queue.Full:
            return
        self.dropped.clear()

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 (stdlib name)
        self.failures += 1  # a logging failure is counted, never raised into the caller


class BoundedQueueListener(logging.handlers.QueueListener):
    def stop(self, timeout: float = 5.0) -> None:
        """Drain what is queued, then stop; give up after ``timeout`` if a writer is stuck."""
        thread = getattr(self, "_thread", None)
        if thread is None:
            return
        while True:
            try:
                self.enqueue_sentinel()
                break
            except queue.Full:  # make room for the stop marker by discarding the oldest record
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    pass
        thread.join(timeout)
        self._thread = None


class PrunedRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A size-rotated file, private to the owner, whose old files also expire by age."""

    def __init__(self, path: Path, *, max_bytes: int, backups: int, retention_days: int) -> None:
        self.retention_seconds = retention_days * 86400
        super().__init__(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        self.prune()

    def _open(self):  # owner-only permissions, even if the umask is permissive
        descriptor = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(descriptor, "a", encoding="utf-8")

    def doRollover(self) -> None:  # noqa: N802 (stdlib name)
        super().doRollover()
        self.prune()

    def prune(self) -> None:
        """Delete rotated files older than the retention period (never the active file)."""
        cutoff = time.time() - self.retention_seconds
        base = Path(self.baseFilename)
        for old in base.parent.glob(base.name + ".*"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        # Disk full, permissions, a removed directory: note it once on stderr, never raise.
        if not getattr(self, "_warned", False):
            self._warned = True
            print("rehearse_ai: operational log file is not writable; continuing", file=sys.stderr)
