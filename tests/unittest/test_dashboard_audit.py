"""Tests for durable dashboard audit hooks."""

import asyncio
import threading
from unittest.mock import AsyncMock

import pytest

from pr_agent.dashboard import audit
from pr_agent.dashboard.storage import DashboardStorage
from pr_agent.tools import pr_reviewer


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


def test_failed_audit_persists_usage_and_status_in_one_call(monkeypatch):
    captured = {}

    class RecordingStorage:
        def fail_review(self, request_id, error_message, **fields):
            captured.update({"request_id": request_id, "error_message": error_message, **fields})

        def set_review_usage(self, *args, **kwargs):
            raise AssertionError("failure audit must use one terminal transaction")

    monkeypatch.setattr(audit, "_run_audit", lambda: RecordingStorage())
    monkeypatch.setattr(
        audit, "_run_payload_fields",
        lambda: {"model": "m", "reasoning_effort": "high", "prompt_tokens": 1,
                 "completion_tokens": 2, "total_tokens": 3, "duration_ms": 4})

    asyncio.run(audit.review_failed("request-id", "model failed"))

    assert captured["request_id"] == "request-id"
    assert captured["error_message"] == "model failed"
    assert captured["total_tokens"] == 3


def test_initial_audit_write_runs_off_event_loop(monkeypatch):
    main_thread = threading.get_ident()
    called = {}

    class GitProvider:
        pr = type("PR", (), {"title": "Title"})()
        repo = None
        id_project = "group/subgroup/repo"
        id_mr = 42

        def get_head_commit_sha(self):
            called["metadata_thread"] = threading.get_ident()
            return "abc123"

    class Reviewer:
        pr_url = "https://github.com/a/b/pull/1"
        git_provider = GitProvider()

    def fake_started(**kwargs):
        called["storage_thread"] = threading.get_ident()
        called["kwargs"] = kwargs
        return "request-id"

    monkeypatch.setattr(audit, "review_started", fake_started)
    request_id = asyncio.run(pr_reviewer._audit_started(Reviewer()))

    assert request_id == "request-id"
    assert called["metadata_thread"] != main_thread
    assert called["storage_thread"] != main_thread
    assert called["kwargs"]["repo_name"] == "group/subgroup/repo"
    assert called["kwargs"]["pr_number"] == 42


def test_metadata_fields_are_extracted_independently(monkeypatch):
    captured = {}

    class BrokenPR:
        @property
        def title(self):
            raise RuntimeError("provider title unavailable")

    class GitProvider:
        pr = BrokenPR()
        id_project = "group/subgroup/repo"
        id_mr = 77

        def get_head_commit_sha(self):
            return "sha-after-title-error"

    class Reviewer:
        pr_url = "https://gitlab.example/group/subgroup/repo/-/merge_requests/77"
        git_provider = GitProvider()

    def fake_started(**kwargs):
        captured.update(kwargs)
        return "request-id"

    monkeypatch.setattr(audit, "review_started", fake_started)

    assert asyncio.run(pr_reviewer._audit_started(Reviewer())) == "request-id"
    assert captured["pr_title"] == ""
    assert captured["commit_sha"] == "sha-after-title-error"
    assert captured["repo_name"] == "group/subgroup/repo"
    assert captured["pr_number"] == 77


def test_audit_startup_cancellation_closes_late_running_record(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    audit_failed = AsyncMock()

    class Reviewer:
        pr_url = "https://github.com/a/b/pull/1"
        git_provider = type("Provider", (), {"pr": {"title": "Title"}})()

    def delayed_started(**kwargs):
        started.set()
        assert release.wait(timeout=2)
        return "late-request-id"

    monkeypatch.setattr(audit, "review_started", delayed_started)
    monkeypatch.setattr(pr_reviewer, "_audit_failed", audit_failed)

    async def scenario():
        task = asyncio.create_task(pr_reviewer._audit_started(Reviewer()))
        await asyncio.to_thread(started.wait, 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    error = audit_failed.await_args.args[1]
    assert audit_failed.await_args.args[0] == "late-request-id"
    assert str(error) == "review task cancelled during audit startup"
