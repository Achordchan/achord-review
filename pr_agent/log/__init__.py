import os
os.environ["AUTO_CAST_FOR_DYNACONF"] = "false"
import json
import logging
import queue
import sys
import threading
from enum import Enum
from logging.handlers import RotatingFileHandler

from loguru import logger


class LoggingFormat(str, Enum):
    CONSOLE = "CONSOLE"
    JSON = "JSON"


# Human-readable line for the dashboard log file. The review id sits early in the
# line (extra[review_request_id], "-" outside a review) so grepping one run is a
# single token, and the file stays readable in the panel's log view.
REVIEW_LOG_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <7} | {extra[review_request_id]} | "
    "{name}:{function}:{line} - {message}"
)
# Bound the review log's in-memory backlog so a stalled data volume drops records
# rather than blocking the review path. 10k lines is minutes of busy logging.
REVIEW_LOG_QUEUE_MAXSIZE = 10_000
REVIEW_LOG_ROTATION_BYTES = 10 * 1024 * 1024
REVIEW_LOG_BACKUP_COUNT = 3


def json_format(record: dict) -> str:
    return record["message"]


def analytics_filter(record: dict) -> bool:
    return record.get("extra", {}).get("analytics", False)


def inv_analytics_filter(record: dict) -> bool:
    return not record.get("extra", {}).get("analytics", False)


def setup_logger(level: str = "INFO", fmt: LoggingFormat = LoggingFormat.CONSOLE):
    level: int = logging.getLevelName(level.upper())
    if type(level) is not int:
        level = logging.INFO

    if fmt == LoggingFormat.JSON and os.getenv("LOG_SANE", "0").lower() == "0":  # better debugging github_app
        logger.remove(None)
        logger.add(
            sys.stdout,
            filter=inv_analytics_filter,
            level=level,
            format="{message}",
            colorize=False,
            serialize=True,
        )
    elif fmt == LoggingFormat.CONSOLE: # does not print the 'extra' fields
        logger.remove(None)
        logger.add(sys.stdout, level=level, colorize=True, filter=inv_analytics_filter)

    # Imported lazily so `from pr_agent.log import get_logger` does not pull
    # in config_loader while this package is still initializing. config_loader
    # loads settings via custom_merge_loader, which itself imports get_logger.
    from pr_agent.config_loader import get_settings

    log_folder = get_settings().get("CONFIG.ANALYTICS_FOLDER", "")
    if log_folder:
        pid = os.getpid()
        log_file = os.path.join(log_folder, f"pr-agent.{pid}.log")
        logger.add(
            log_file,
            filter=analytics_filter,
            level=level,
            format="{message}",
            colorize=False,
            serialize=True,
        )

    return logger


class _ReviewLogWriter:
    """A bounded, non-blocking file writer running on its own daemon thread.

    Records are handed off with put_nowait and DROPPED when the queue is full, so
    a stalled /app/data volume can never block the caller — reviews run on the
    event loop and must never wait on filesystem I/O. loguru's own enqueue is not
    enough: its queue is backed by an OS pipe that blocks the caller once it fills
    while the writer is stuck. A background thread drains the queue into a
    size-rotated file via the stdlib RotatingFileHandler.
    """

    def __init__(self, path: str):
        self._queue: queue.Queue = queue.Queue(maxsize=REVIEW_LOG_QUEUE_MAXSIZE)
        self.dropped = 0
        self._handler = RotatingFileHandler(
            path, maxBytes=REVIEW_LOG_ROTATION_BYTES, backupCount=REVIEW_LOG_BACKUP_COUNT,
            encoding="utf-8", delay=True)
        self._thread = threading.Thread(
            target=self._run, name="review-log-writer", daemon=True)
        self._thread.start()

    def write(self, message) -> None:
        """loguru sink: hand the formatted line off without ever blocking."""
        try:
            self._queue.put_nowait(str(message))
        except queue.Full:
            self.dropped += 1  # disk can't keep up — drop rather than stall the review

    def _run(self) -> None:
        while True:
            message = self._queue.get()
            if message is None:
                return
            try:
                # Reuse RotatingFileHandler's size rollover; write the preformatted
                # loguru line as-is (args=None, so no %-interpolation of the message).
                self._handler.emit(logging.LogRecord(
                    "achord-review", logging.INFO, __file__, 0,
                    message.rstrip("\n"), None, None))
            except Exception:
                pass  # a log write must never escape the writer thread

    def stop(self) -> None:
        """Drain the queue, stop the thread and close the file (idempotent-safe)."""
        self._queue.put(None)
        self._thread.join(timeout=2)
        try:
            self._handler.close()
        except Exception:
            pass


# The review sink's handler id and writer in THIS process, so a repeated enable
# replaces rather than stacks them, and so a re-enable retires the old writer
# thread. Never set in the gunicorn master: the writer thread cannot survive fork,
# so the sink is installed only from the worker (post_worker_init), and these stay
# None everywhere else.
_REVIEW_SINK_ID = None
_REVIEW_LOG_WRITER: "_ReviewLogWriter | None" = None


def enable_review_log_sink(level="INFO"):
    """Mirror logs to a rotating file the dashboard can read in-container.

    achord-review otherwise logs only to stdout, which Docker keeps under a host
    path the container itself cannot read, so the panel's log view stays empty and
    every lookup means SSHing to the host. When ACHORD_REVIEW_LOG_FILE is set the
    app writes its logs to a file under that path, one per process (workers share
    the base name, each writing "<stem>.<pid><ext>"): a per-process file means
    cross-process rotation never renames a file out from under another worker's
    open handle. The write goes through a bounded, drop-on-full queue and a
    dedicated writer thread (see _ReviewLogWriter), so a slow /app/data drops
    records instead of blocking the review event loop. Best-effort: logging must
    never take the process down, and the stdout sink keeps working if this fails.
    Call only from a worker/single process, never the preloaded master.
    """
    global _REVIEW_SINK_ID, _REVIEW_LOG_WRITER
    base_path = os.environ.get("ACHORD_REVIEW_LOG_FILE", "").strip()
    if not base_path:
        return None
    try:
        # A default so REVIEW_LOG_FORMAT's {extra[review_request_id]} always
        # resolves; the reviewer's contextualize overrides it per run.
        logger.configure(extra={"review_request_id": "-"})
        if _REVIEW_SINK_ID is not None:
            try:
                logger.remove(_REVIEW_SINK_ID)
            except ValueError:
                pass  # already gone (e.g. a prior setup_logger removed all sinks)
            _REVIEW_SINK_ID = None
        if _REVIEW_LOG_WRITER is not None:
            _REVIEW_LOG_WRITER.stop()
            _REVIEW_LOG_WRITER = None
        stem, ext = os.path.splitext(base_path)
        per_process_path = f"{stem}.{os.getpid()}{ext or '.log'}"
        directory = os.path.dirname(per_process_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        _REVIEW_LOG_WRITER = _ReviewLogWriter(per_process_path)
        _REVIEW_SINK_ID = logger.add(
            _REVIEW_LOG_WRITER.write,
            level=level,
            format=REVIEW_LOG_FORMAT,
            filter=inv_analytics_filter,
            catch=True,
            colorize=False,
        )
        return _REVIEW_SINK_ID
    except Exception as e:
        logger.warning(f"Review log file sink not enabled ({base_path!r}), error: {e}")
        return None


def get_logger(*args, **kwargs):
    return logger
