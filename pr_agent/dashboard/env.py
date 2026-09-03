"""Safe parsing for the dashboard's integer environment tunables.

Every value here is read once at module import. A bare int() would turn an
operator typo (``90d``, ``one``) into an ImportError that takes the whole
dashboard offline — github_app.py mounts the routes inside a try/except, so
the panel would simply disappear. Parsing degrades to a documented default
instead, and a lower bound keeps nonsensical values from corrupting the
behaviour they drive.
"""

import os

from pr_agent.log import get_logger


def bounded_env_int(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return max(minimum, default)
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError) as e:
        get_logger().warning(f"Invalid {name}={raw!r}; using {default}, error: {e}")
        return max(minimum, default)
