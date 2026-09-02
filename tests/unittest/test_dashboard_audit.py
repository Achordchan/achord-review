"""Tests for durable dashboard audit hooks."""

import asyncio

from pr_agent.dashboard import audit
from pr_agent.dashboard.storage import DashboardStorage


def test_terminal_audit_write_is_complete_before_await_returns(tmp_path, monkeypatch):
    storage = DashboardStorage(db_path=str(tmp_path / "audit.db"))
    storage.initialize()
    request_id = storage.create_review(repo_name="a/b", pr_number=1, pr_url="u")
    monkeypatch.setattr(audit, "_run_audit", lambda: storage)
    monkeypatch.setattr(
        audit, "_run_payload_fields",
        lambda: {"model": "m", "reasoning_effort": "high", "prompt_tokens": 1,
                 "completion_tokens": 2, "total_tokens": 3, "duration_ms": 4})

    asyncio.run(audit.review_skipped(request_id, "no files"))

    row = storage.get_review_by_request_id(request_id)
    assert row["status"] == "SKIPPED"
    assert row["total_tokens"] == 3
