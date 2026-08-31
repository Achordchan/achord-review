import errno
import hashlib
import os
import tempfile
import time
from contextlib import contextmanager
from typing import Iterator

from pr_agent.log import get_logger

try:
    import fcntl
except ImportError:  # not POSIX - the lock degrades to a no-op, see publish_lock
    fcntl = None

LOCK_TIMEOUT_SECONDS = 30.0
_POLL_SECONDS = 0.05


def _lock_path(key: str) -> str:
    directory = os.path.join(tempfile.gettempdir(), "pr-agent-publish-locks")
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, f"{hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]}.lock")


@contextmanager
def publish_lock(key: str, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[bool]:
    """Serialise a read-then-publish section across processes, keyed by `key`.

    Reading what has already been published and then publishing is two operations, and a
    guard built on it is only as good as the gap between them. The triggers that race here
    - a push and a mention of the bot seconds later - are handled by different gunicorn
    workers of one container, so an in-process lock cannot see both. The filesystem they
    share can: flock is held per open file description and is arbitrated by the kernel,
    which makes this an actual mutex rather than another read-then-act.

    Yields True when the lock is held, False when it is not - and the body runs either
    way. Failing open is deliberate: a duplicated review is a bad day, a missed one reads
    as the bot being broken.
    """
    if fcntl is None:
        get_logger().debug("flock is unavailable on this platform; publishing without the lock")
        yield False
        return

    fd = None
    held = False
    try:
        fd = os.open(_lock_path(key), os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except OSError as e:
                if e.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    get_logger().warning(
                        f"Timed out after {timeout}s waiting for the publish lock; publishing unlocked")
                    break
                time.sleep(_POLL_SECONDS)
    except Exception as e:
        get_logger().warning(f"Could not take the publish lock, publishing unlocked, error: {e}")

    try:
        yield held
    finally:
        if fd is not None:
            if held:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except Exception as e:
                    get_logger().warning(f"Failed to release the publish lock, error: {e}")
            try:
                os.close(fd)
            except Exception:
                pass
