"""Tests for durable dashboard audit hooks."""

import asyncio
import threading

import pytest

from pr_agent.dashboard import audit
from pr_agent.dashboard.storage import DashboardStorage
from pr_agent.tools import pr_reviewer


@pytest.mark.parametrize("value", ["", "2s", "nan", "inf", "-inf"])
def test_invalid_audit_start_timeout_uses_safe_default(value, monkeypatch):
    monkeypatch.setenv("DASHBOARD_AUDIT_START_TIMEOUT_SECONDS", value)

    assert pr_reviewer._load_audit_start_timeout_seconds() == 2.0


def test_audit_start_timeout_preserves_supported_lower_bound(monkeypatch):
    monkeypatch.setenv("DASHBOARD_AUDIT_START_TIMEOUT_SECONDS", "-5")

    assert pr_reviewer._load_audit_start_timeout_seconds() == 0.1


def test_audit_start_timeout_has_a_hard_upper_bound(monkeypatch):
    monkeypatch.setenv("DASHBOARD_AUDIT_START_TIMEOUT_SECONDS", "999")

    assert pr_reviewer._load_audit_start_timeout_seconds() == 30.0


def test_started_audit_preserves_a_reserved_request_id(tmp_path, monkeypatch):
    storage = DashboardStorage(db_path=str(tmp_path / "audit.db"))
    storage.initialize()
    monkeypatch.setattr(audit, "_run_audit", lambda: storage)

    request_id = audit.review_started(
        "https://github.com/a/b/pull/1", request_id="reserved-request-id")

    assert request_id == "reserved-request-id"
    assert storage.get_review_by_request_id(request_id)["status"] == "RUNNING"


@pytest.mark.parametrize(("url", "expected"), [
    ("https://github.com/owner/repo/pull/1", ("owner/repo", 1)),
    ("https://api.github.com/repos/owner/repo/pulls/2", ("owner/repo", 2)),
    ("https://gitea.example/owner/repo/pulls/3", ("owner/repo", 3)),
    ("https://gitlab.example/group/subgroup/repo/-/merge_requests/4",
     ("group/subgroup/repo", 4)),
    ("https://gitlab.example/projects/123/-/merge_requests/5", ("123", 5)),
    ("https://bitbucket.org/workspace/repo/pull-requests/6", ("workspace/repo", 6)),
    ("https://stash.example/projects/PROJ/repos/repo/pull-requests/7", ("PROJ/repo", 7)),
    ("https://stash.example/users/alice/repos/repo/pull-requests/8", ("~alice/repo", 8)),
    ("https://dev.azure.com/org/Dev%20Project/_git/repo%20name/pullrequest/9",
     ("Dev Project/repo name", 9)),
    ("https://us-east-1.console.aws.amazon.com/codesuite/codecommit/repositories/repo/pull-requests/10",
     ("repo", 10)),
])
def test_audit_pr_url_parser_supports_provider_specific_paths(url, expected):
    assert audit._parse_pr_url(url) == expected


def test_audit_pr_url_parser_strips_configured_gitlab_subpath(monkeypatch):
    settings = audit.get_settings()
    previous_url = settings.gitlab.url
    try:
        settings.gitlab.url = "https://gitlab.example/gitlab"

        assert audit._parse_pr_url(
            "https://gitlab.example/gitlab/group/repo/-/merge_requests/11") == ("group/repo", 11)
    finally:
        settings.gitlab.url = previous_url


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


def test_finished_audit_normalizes_valid_severity_before_storage(tmp_path, monkeypatch):
    storage = DashboardStorage(db_path=str(tmp_path / "audit.db"))
    storage.initialize()
    request_id = storage.create_review(repo_name="a/b", pr_number=1, pr_url="u")
    monkeypatch.setattr(audit, "_run_audit", lambda: storage)

    asyncio.run(audit.review_finished(
        request_id,
        issues=[{
            "severity": "p1",
            "issue_header": "lowercase finding",
            "start_line": str(2 ** 80),
            "end_line": -1,
        }],
    ))

    row = storage.get_review_by_request_id(request_id)
    detail = storage.get_review_detail(row["id"])
    assert detail["issues"][0]["severity"] == "P1"
    assert detail["issues"][0]["relevant_lines_start"] is None
    assert detail["issues"][0]["relevant_lines_end"] is None
    assert storage.stats_overview()["p0_p1_blocked"] == 1


def test_review_heartbeat_loop_refreshes_until_cancelled(monkeypatch):
    touched = asyncio.Event()

    async def fake_heartbeat(request_id):
        assert request_id == "request-id"
        touched.set()

    monkeypatch.setattr(audit, "review_heartbeat", fake_heartbeat)

    async def scenario():
        task = asyncio.create_task(
            audit.review_heartbeat_loop("request-id", interval_seconds=0))
        await touched.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.gather(task)

    asyncio.run(scenario())


def test_reviewer_owns_and_stops_heartbeat_task(monkeypatch):
    started = asyncio.Event()

    async def fake_loop(request_id):
        assert request_id == "request-id"
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(audit, "review_heartbeat_loop", fake_loop)

    async def scenario():
        task = pr_reviewer._start_audit_heartbeat("request-id")
        assert task is not None
        await started.wait()
        await pr_reviewer._stop_audit_heartbeat(task)
        assert task.cancelled()

    asyncio.run(scenario())


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
        return kwargs["request_id"]

    monkeypatch.setattr(audit, "review_started", fake_started)
    request_id = asyncio.run(pr_reviewer._audit_started(Reviewer()))

    assert request_id == called["kwargs"]["request_id"]
    assert "metadata_thread" not in called
    assert called["storage_thread"] != main_thread
    assert called["kwargs"]["pr_url"] == "https://github.com/a/b/pull/1"


def test_audit_start_does_not_access_provider_metadata(monkeypatch):
    captured = {}

    class BrokenPR:
        @property
        def title(self):
            raise AssertionError("audit startup must not access provider title")

    class GitProvider:
        pr = BrokenPR()
        id_project = "group/subgroup/repo"
        id_mr = 77

        def get_head_commit_sha(self):
            raise AssertionError("audit startup must not access provider SHA")

    class Reviewer:
        pr_url = "https://gitlab.example/group/subgroup/repo/-/merge_requests/77"
        git_provider = GitProvider()

    def fake_started(**kwargs):
        captured.update(kwargs)
        return kwargs["request_id"]

    monkeypatch.setattr(audit, "review_started", fake_started)

    request_id = asyncio.run(pr_reviewer._audit_started(Reviewer()))
    assert request_id == captured["request_id"]
    assert captured["pr_url"] == Reviewer.pr_url
    assert "pr_title" not in captured
    assert "commit_sha" not in captured


def test_audit_start_timeout_preserves_id_and_serializes_terminal_write(monkeypatch):
    storage_started = threading.Event()
    release_storage = threading.Event()
    terminal = {}
    reserved = {}

    class Reviewer:
        pr_url = "https://github.com/a/b/pull/1"
        git_provider = type("Provider", (), {"pr": {"title": "Title"}})()

    def delayed_started(**kwargs):
        storage_started.set()
        assert release_storage.wait(timeout=2)
        return kwargs["request_id"]

    async def scenario():
        terminal_written = asyncio.Event()

        async def fake_skipped(request_id, reason):
            terminal["request_id"] = request_id
            terminal["reason"] = reason
            terminal_written.set()

        monkeypatch.setattr(pr_reviewer, "AUDIT_START_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(audit, "review_started", delayed_started)
        monkeypatch.setattr(audit, "review_skipped", fake_skipped)

        start_task = asyncio.create_task(pr_reviewer._audit_started(Reviewer()))
        assert await asyncio.to_thread(storage_started.wait, 1)
        request_id = await asyncio.wait_for(start_task, 0.2)
        assert request_id
        reserved["request_id"] = request_id

        terminal_task = asyncio.create_task(
            pr_reviewer._audit_skipped(request_id, "review completed after slow audit start"))
        await asyncio.sleep(0)
        assert terminal == {}
        assert not terminal_task.done()

        release_storage.set()
        await asyncio.wait_for(terminal_written.wait(), 1)
        assert await terminal_task is None

    asyncio.run(scenario())
    assert terminal == {
        "request_id": reserved["request_id"],
        "reason": "review completed after slow audit start",
    }


def test_audit_startup_cancellation_closes_late_running_record(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    closed = {}

    class Reviewer:
        pr_url = "https://github.com/a/b/pull/1"
        git_provider = type("Provider", (), {"pr": {"title": "Title"}})()

    def delayed_started(**kwargs):
        started.set()
        assert release.wait(timeout=2)
        return kwargs["request_id"]

    async def scenario():
        record_closed = asyncio.Event()

        async def fake_failed(request_id, error):
            closed["request_id"] = request_id
            closed["error"] = str(error)
            record_closed.set()

        monkeypatch.setattr(audit, "review_started", delayed_started)
        monkeypatch.setattr(pr_reviewer, "_audit_failed", fake_failed)

        task = asyncio.create_task(pr_reviewer._audit_started(Reviewer()))
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 0.2)
        assert closed == {}

        release.set()
        await asyncio.wait_for(record_closed.wait(), 1)

    asyncio.run(scenario())
    assert closed["request_id"]
    assert closed["error"] == "review task cancelled during audit startup"
