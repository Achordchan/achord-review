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


def test_finished_audit_persists_review_comment_url(tmp_path, monkeypatch):
    storage = DashboardStorage(db_path=str(tmp_path / "audit.db"))
    storage.initialize()
    request_id = storage.create_review(repo_name="a/b", pr_number=7, pr_url="u")
    monkeypatch.setattr(audit, "_run_audit", lambda: storage)

    url = "https://github.com/a/b/pull/7#pullrequestreview-1"
    asyncio.run(audit.review_finished(request_id, verdict="COMMENT", review_comment_url=url))

    detail = storage.get_review_detail(storage.get_review_by_request_id(request_id)["id"])
    assert detail["review_comment_url"] == url


def test_heartbeat_loop_invokes_cancel_callback_when_flag_set(tmp_path, monkeypatch):
    storage = DashboardStorage(db_path=str(tmp_path / "audit.db"))
    storage.initialize()
    request_id = storage.create_review(repo_name="a/b", pr_number=9, pr_url="u")
    review_id = storage.get_review_by_request_id(request_id)["id"]
    monkeypatch.setattr(audit, "_run_audit", lambda: storage)
    calls = {"n": 0}

    async def scenario():
        storage.cancel_review(review_id)  # admin flags the stop
        # With the flag set the loop must invoke the callback once and return,
        # so wait_for never times out.
        await asyncio.wait_for(
            audit.review_heartbeat_loop(
                request_id, interval_seconds=0.01,
                on_cancel_requested=lambda: calls.__setitem__("n", calls["n"] + 1)),
            timeout=2)

    asyncio.run(scenario())
    assert calls["n"] == 1


def test_heartbeat_loop_without_flag_does_not_cancel(tmp_path, monkeypatch):
    storage = DashboardStorage(db_path=str(tmp_path / "audit.db"))
    storage.initialize()
    request_id = storage.create_review(repo_name="a/b", pr_number=10, pr_url="u")
    monkeypatch.setattr(audit, "_run_audit", lambda: storage)
    calls = {"n": 0}

    async def scenario():
        # No flag: the loop beats forever, so it is cancelled by the timeout and
        # the callback is never invoked.
        await audit.review_heartbeat_loop(
            request_id, interval_seconds=0.01,
            on_cancel_requested=lambda: calls.__setitem__("n", calls["n"] + 1))

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(asyncio.wait_for(scenario(), timeout=0.2))
    assert calls["n"] == 0


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

    async def fake_loop(request_id, on_cancel_requested=None):
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


def test_audit_metadata_backfills_the_title_and_head_commit(monkeypatch):
    # The dashboard list and detail views read these columns; the audit start
    # cannot fill them because it must not touch the provider.
    recorded = {}

    class GitProvider:
        def get_head_commit_sha(self):
            return "abc123"

    class Reviewer:
        pr_url = "https://github.com/a/b/pull/1"
        git_provider = GitProvider()
        vars = {"title": "Fix the thing"}

    async def fake_metadata(request_id, pr_title="", commit_sha=""):
        recorded.update(request_id=request_id, pr_title=pr_title, commit_sha=commit_sha)

    monkeypatch.setattr(audit, "review_metadata", fake_metadata)
    asyncio.run(pr_reviewer._audit_metadata("request-id", Reviewer()))

    assert recorded == {"request_id": "request-id", "pr_title": "Fix the thing",
                        "commit_sha": "abc123"}


def test_audit_metadata_skips_a_provider_without_the_metadata(monkeypatch):
    calls = []

    class GitProvider:
        def get_head_commit_sha(self):
            raise AssertionError("provider does not expose a head commit")

    class Reviewer:
        pr_url = "https://gitlab.example/g/r/-/merge_requests/7"
        git_provider = GitProvider()
        vars = {}

    async def fake_metadata(request_id, pr_title="", commit_sha=""):
        calls.append(request_id)

    monkeypatch.setattr(audit, "review_metadata", fake_metadata)
    asyncio.run(pr_reviewer._audit_metadata("request-id", Reviewer()))
    asyncio.run(pr_reviewer._audit_metadata("", Reviewer()))

    assert calls == []


def test_review_metadata_leaves_untouched_columns_alone(tmp_path):
    storage = DashboardStorage(db_path=str(tmp_path / "metadata.db"))
    storage.initialize()
    request_id = storage.create_review(repo_name="a/b", pr_number=1, pr_url="u")

    storage.set_review_metadata(request_id, pr_title="Fix the thing", commit_sha="abc123")
    storage.set_review_metadata(request_id, commit_sha="def456")

    row = storage.get_review_by_request_id(request_id)
    assert row["pr_title"] == "Fix the thing"
    assert row["commit_sha"] == "def456"
    assert row["status"] == "RUNNING"


def _pending_audit_start(request_id):
    """Register a start task that never completes, as a hung SQLite volume would."""
    pending = asyncio.create_task(asyncio.sleep(3600))
    pr_reviewer._AUDIT_START_TASKS[request_id] = pending
    return pending


def test_metadata_backfill_gives_up_on_a_pending_audit_start(monkeypatch):
    # The audit start deadline exists so a hung volume cannot stall the review;
    # the backfill runs on that same path and must honour it.
    calls = []

    class Reviewer:
        pr_url = "https://github.com/a/b/pull/1"
        git_provider = type("GitProvider", (), {"get_head_commit_sha": lambda self: "abc123"})()
        vars = {"title": "Fix the thing"}

    async def fake_metadata(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(audit, "review_metadata", fake_metadata)
    monkeypatch.setattr(pr_reviewer, "AUDIT_START_TIMEOUT_SECONDS", 0.01)

    async def scenario():
        pending = _pending_audit_start("request-id")
        try:
            await asyncio.wait_for(
                pr_reviewer._audit_metadata("request-id", Reviewer()), timeout=5)
            assert calls == []
            # still registered, so the terminal write can serialize behind it
            assert pr_reviewer._AUDIT_START_TASKS.get("request-id") is pending
        finally:
            pr_reviewer._AUDIT_START_TASKS.pop("request-id", None)
            pending.cancel()

    asyncio.run(scenario())


def test_cancelled_waiter_leaves_the_pending_audit_start_registered():
    # Dropping it here would let the cancellation path write FAILED before the
    # row exists; the late insert would then strand a RUNNING review.
    async def scenario():
        pending = _pending_audit_start("request-id")
        try:
            waiter = asyncio.create_task(pr_reviewer._wait_for_audit_start("request-id"))
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.gather(waiter)

            assert pr_reviewer._AUDIT_START_TASKS.get("request-id") is pending
        finally:
            pr_reviewer._AUDIT_START_TASKS.pop("request-id", None)
            pending.cancel()

    asyncio.run(scenario())


def test_completed_audit_start_is_retired_after_the_wait():
    async def scenario():
        async def _insert():
            return "request-id"

        pr_reviewer._AUDIT_START_TASKS["request-id"] = asyncio.create_task(_insert())
        try:
            assert await pr_reviewer._wait_for_audit_start("request-id") is True
            assert "request-id" not in pr_reviewer._AUDIT_START_TASKS
        finally:
            pr_reviewer._AUDIT_START_TASKS.pop("request-id", None)

    asyncio.run(scenario())


def test_audit_storage_work_stays_off_the_shared_executor(monkeypatch):
    # A hung /app/data volume must not consume the loop's default pool, which
    # review publication and the dashboard's own queries also use.
    threads = []

    class RecordingStorage:
        def skip_review(self, request_id, reason, **fields):
            threads.append(threading.current_thread().name)

    monkeypatch.setattr(audit, "_run_audit", lambda: RecordingStorage())
    monkeypatch.setattr(audit, "_run_payload_fields", dict)

    async def scenario():
        await audit.review_skipped("request-id", "no files")
        default_pool = await asyncio.to_thread(lambda: threading.current_thread().name)
        return default_pool

    default_pool = asyncio.run(scenario())

    assert threads and all(name.startswith("dashboard-audit") for name in threads)
    assert not default_pool.startswith("dashboard-audit")


def test_audit_start_runs_on_the_dedicated_executor(monkeypatch):
    threads = []

    class Reviewer:
        pr_url = "https://github.com/a/b/pull/1"
        git_provider = None

    def fake_started(**kwargs):
        threads.append(threading.current_thread().name)
        return kwargs["request_id"]

    monkeypatch.setattr(audit, "review_started", fake_started)
    asyncio.run(pr_reviewer._audit_started(Reviewer()))

    assert threads and threads[0].startswith("dashboard-audit")


def test_audit_worker_sees_the_request_scope(monkeypatch):
    # run_in_executor drops contextvars where asyncio.to_thread copied them:
    # the worker reads the request scope for sender/trigger and for the
    # request-scoped settings behind get_settings().
    from starlette_context import request_cycle_context

    captured = {}

    class Reviewer:
        pr_url = "https://github.com/a/b/pull/1"
        git_provider = None

    def fake_started(**kwargs):
        captured.update(kwargs)
        return kwargs["request_id"]

    monkeypatch.setattr(audit, "review_started", fake_started)

    async def scenario():
        with request_cycle_context({"dashboard_sender": "octocat",
                                    "dashboard_trigger_type": "mention"}):
            await pr_reviewer._audit_started(Reviewer())

    asyncio.run(scenario())

    assert captured["sender"] == "octocat"
    assert captured["trigger_type"] == "mention"


def test_stalled_metadata_write_does_not_hold_up_the_review(monkeypatch):
    # The backfill sits on the review's own path, before model generation: a
    # stalled volume must cost it the bounded window and no more.
    release = asyncio.Event()
    finished = asyncio.Event()

    class Reviewer:
        pr_url = "https://github.com/a/b/pull/1"
        git_provider = type("GitProvider", (), {"get_head_commit_sha": lambda self: "abc123"})()
        vars = {"title": "Fix the thing"}

    async def stalled_metadata(request_id, pr_title="", commit_sha=""):
        await release.wait()
        finished.set()

    monkeypatch.setattr(audit, "review_metadata", stalled_metadata)
    monkeypatch.setattr(pr_reviewer, "AUDIT_TERMINAL_TIMEOUT_SECONDS", 0.01)

    async def scenario():
        await asyncio.wait_for(pr_reviewer._audit_metadata("request-id", Reviewer()), timeout=5)
        # the review moved on; the write is still tracked and completes later
        assert not finished.is_set()
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=5)

    asyncio.run(scenario())
