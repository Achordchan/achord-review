import os
os.environ["AUTO_CAST_FOR_DYNACONF"] = "false"
import json
import logging
import sys
from enum import Enum

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


# The review log sink's handler id in THIS process, so a repeated enable replaces
# rather than stacks it. Never set in the gunicorn master: the sink is enqueued
# (its own writer thread), and a writer thread cannot survive fork — a worker that
# inherited one would deadlock joining it on the next logger.remove(). So it is
# added only from the worker (post_fork), and this stays None everywhere else.
_REVIEW_SINK_ID = None


def enable_review_log_sink(level="INFO"):
    """Mirror logs to a rotating file the dashboard can read in-container.

    achord-review otherwise logs only to stdout, which Docker keeps under a host
    path the container itself cannot read, so the panel's log view stays empty and
    every lookup means SSHing to the host. When ACHORD_REVIEW_LOG_FILE is set the
    app writes its logs to a file under that path, one per process (workers share
    the base name, each writing "<stem>.<pid><ext>"): a per-process file means
    cross-process rotation never renames a file out from under another worker's
    open handle. enqueue=True keeps the filesystem write off the caller — reviews
    run on the event loop, and a slow /app/data must not block them. Best-effort:
    logging must never take the process down, and the stdout sink keeps working if
    this fails. Call only from a worker/single process, never the preloaded master.
    """
    global _REVIEW_SINK_ID
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
        stem, ext = os.path.splitext(base_path)
        per_process_path = f"{stem}.{os.getpid()}{ext or '.log'}"
        directory = os.path.dirname(per_process_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        _REVIEW_SINK_ID = logger.add(
            per_process_path,
            level=level,
            format=REVIEW_LOG_FORMAT,
            filter=inv_analytics_filter,
            rotation="10 MB",
            retention=3,
            enqueue=True,
            catch=True,
            colorize=False,
        )
        return _REVIEW_SINK_ID
    except Exception as e:
        logger.warning(f"Review log file sink not enabled ({base_path!r}), error: {e}")
        return None


def get_logger(*args, **kwargs):
    return logger
