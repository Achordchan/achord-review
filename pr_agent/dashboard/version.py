"""Single source of truth for the achord-review control-panel version.

This is the panel's own semantic version, deliberately independent of the
upstream PR-Agent version in pyproject.toml. It is baked into the image (this
module ships inside the pr_agent package) so the running container always
reports the code it is actually executing.

Release flow: bump APP_VERSION in the same PR that ships the change, then tag
the merge commit `v<APP_VERSION>`. The panel compares this baked value against
the latest GitHub release tag to decide whether an update is available.
"""

import os

APP_VERSION = "0.0.9"


def get_app_version() -> str:
    """Return the running panel version; an env override wins for custom builds."""
    return os.environ.get("DASHBOARD_VERSION", "").strip() or APP_VERSION
