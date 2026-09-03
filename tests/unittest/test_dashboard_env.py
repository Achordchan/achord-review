"""Tests for the dashboard integer environment parsing (pr_agent/dashboard/env.py)."""

import pytest

from pr_agent.dashboard.env import bounded_env_int

VAR = "DASHBOARD_TEST_TUNABLE"


@pytest.mark.parametrize(("raw", "expected"), [
    (None, 90),          # unset falls back to the default
    ("120", 120),        # a valid override is honoured
    ("0", 1),            # below the bound is clamped, not rejected
    ("-7", 1),
    ("90d", 90),         # a realistic typo degrades to the default
    ("one", 90),
    ("", 90),
    ("  ", 90),
    ("12.5", 90),
])
def test_bounded_env_int_never_raises_on_operator_input(monkeypatch, raw, expected):
    monkeypatch.delenv(VAR, raising=False)
    if raw is not None:
        monkeypatch.setenv(VAR, raw)

    assert bounded_env_int(VAR, 90, 1) == expected


def test_bounded_env_int_keeps_whitespace_padded_numbers(monkeypatch):
    # int() already tolerates surrounding whitespace; keep that behaviour.
    monkeypatch.setenv(VAR, " 42 ")

    assert bounded_env_int(VAR, 90, 1) == 42
