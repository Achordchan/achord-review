import os

# litellm fetches the latest model map from GitHub at import time and only falls back to
# the copy bundled with the pinned version when that fails. Model metadata then changes
# under a fixed litellm pin, and tests that depend on it pass or fail with the day's
# upstream data: in 2026-11 the remote map dropped "openrouter/google/gemini-2.5-pro",
# litellm stopped rejecting reasoning_effort for it, and
# test_litellm_requires_openrouter_reasoning_in_extra_body broke on every branch.
# Unit tests run against the bundled map so they test the pinned version, offline.
# Set before any test module imports litellm; an explicit value in the environment wins.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_run_details():
    """Start each test from a clean run-details ContextVar and restore it.

    The collector lives in a module-level ContextVar, so a test that leaves
    details behind would otherwise be visible to whichever test runs next.
    """
    from pr_agent.algo import run_details

    token = run_details._run_details.set(None)
    yield
    run_details._run_details.reset(token)
