"""Tests for the dashboard storage layer (pr_agent/dashboard/storage.py)."""

import os
import stat
import time

import pytest

import pr_agent.dashboard.storage as storage_module

DashboardStorage = storage_module.DashboardStorage
STALE_CLEANUP_INTERVAL_SECONDS = storage_module.STALE_CLEANUP_INTERVAL_SECONDS


@pytest.fixture()
def storage(tmp_path):
    db_path = os.path.join(str(tmp_path), "test_review.db")
    store = DashboardStorage(db_path=db_path)
    store.initialize()
    return store


def _seed_review(storage, repo="octo/repo", pr=1, status_complete=True, verdict="APPROVE",
                 severities=("P0",), tokens=100):
    request_id = storage.create_review(
        repo_name=repo, pr_number=pr, pr_url=f"https://github.com/{repo}/pull/{pr}",
        pr_title="Test PR", sender="alice", trigger_type="mention")
    assert request_id
    if status_complete:
        storage.complete_review(request_id, verdict=verdict, verdict_reason="ok",
                                markdown_output="# report")
        for sev in severities:
            storage.add_review_issues(request_id, [{
                "severity": sev, "relevant_file": "a.py",
                "relevant_lines_start": 1, "relevant_lines_end": 2,
                "issue_summary": "boom", "suggestion": "fix it",
            }])
    storage.set_review_usage(request_id, model="openai/gpt-test", reasoning_effort="high",
                             prompt_tokens=tokens, completion_tokens=tokens,
                             total_tokens=2 * tokens, duration_ms=1234)
    return request_id


class TestReviewsCrud:
    def test_create_returns_unique_request_ids(self, storage):
        id1 = storage.create_review(repo_name="r", pr_number=1, pr_url="u")
        id2 = storage.create_review(repo_name="r", pr_number=1, pr_url="u")
        assert id1 and id2 and id1 != id2

    def test_create_returns_empty_id_when_insert_fails(self, storage, monkeypatch):
        monkeypatch.setattr(storage, "_write", lambda *args, **kwargs: None)
        assert storage.create_review(repo_name="r", pr_number=1, pr_url="u") == ""

    def test_complete_and_fail_transitions(self, storage):
        request_id = _seed_review(storage, status_complete=False)
        row = storage.get_review_by_request_id(request_id)
        assert row["status"] == "RUNNING"
        storage.complete_review(request_id, verdict="COMMENT")
        row = storage.get_review_by_request_id(request_id)
        assert row["status"] == "COMPLETED"
        assert row["verdict"] == "COMMENT"

        request_id2 = storage.create_review(repo_name="r", pr_number=2, pr_url="u2")
        storage.fail_review(request_id2, "timeout")
        row2 = storage.get_review_by_request_id(request_id2)
        assert row2["status"] == "FAILED"
        assert "timeout" in row2["error_message"]

    def test_skip_review_closes_running_record(self, storage):
        request_id = storage.create_review(repo_name="r", pr_number=3, pr_url="u3")
        storage.skip_review(request_id, "PR has no files")
        row = storage.get_review_by_request_id(request_id)
        assert row["status"] == "SKIPPED"
        assert "no files" in row["error_message"]
        assert row["completed_at"] is not None

    def test_fail_review_preserves_usage_without_a_prerequisite_read(self, storage, monkeypatch):
        request_id = storage.create_review(repo_name="r", pr_number=3, pr_url="u3")
        storage.set_review_usage(
            request_id, model="openai/gpt-x", prompt_tokens=10,
            completion_tokens=5, total_tokens=15, duration_ms=250)
        monkeypatch.setattr(storage, "get_review_by_request_id", lambda *args, **kwargs: None)

        storage.fail_review(request_id, "timeout")

        row = storage._read("SELECT * FROM reviews WHERE request_id = ?", (request_id,))[0]
        assert row["status"] == "FAILED"
        assert row["total_tokens"] == 15
        assert row["duration_ms"] == 250

    def test_finish_review_atomic_writes_issues_and_status(self, storage):
        request_id = _seed_review(storage, status_complete=False)
        storage.finish_review(
            request_id,
            [{"severity": "P0", "relevant_file": "a.py", "relevant_lines_start": 1,
              "relevant_lines_end": 2, "issue_summary": "boom", "suggestion": "fix"}],
            verdict="REQUEST_CHANGES", markdown_output="# r")
        detail = storage.get_review_detail(storage.get_review_by_request_id(request_id)["id"])
        assert detail["status"] == "COMPLETED"
        assert len(detail["issues"]) == 1
        assert detail["verdict"] == "REQUEST_CHANGES"

    def test_finish_review_accepts_usage_fields(self, storage):
        """finish_review takes the run's live usage (audit.py passes **fields)."""
        request_id = storage.create_review(repo_name="r", pr_number=4, pr_url="u4")
        storage.finish_review(
            request_id, [], verdict="APPROVE",
            model="openai/gpt-x", reasoning_effort="high",
            prompt_tokens=111, completion_tokens=22, total_tokens=133, duration_ms=4567)
        row = storage.get_review_by_request_id(request_id)
        assert row["status"] == "COMPLETED"
        assert row["model"] == "openai/gpt-x"
        assert row["reasoning_effort"] == "high"
        assert row["prompt_tokens"] == 111
        assert row["total_tokens"] == 133
        assert row["duration_ms"] == 4567

    def test_review_payloads_are_truncated_to_configured_limit(self, storage, monkeypatch):
        monkeypatch.setattr(storage_module, "MAX_REVIEW_PAYLOAD_BYTES", 128)
        request_id = storage.create_review(repo_name="r", pr_number=7, pr_url="u7")
        storage.finish_review(
            request_id, [], markdown_output="m" * 1000, raw_prediction="r" * 1000)
        row = storage.get_review_by_request_id(request_id)
        assert len(row["markdown_output"].encode()) <= 128
        assert len(row["raw_prediction"].encode()) <= 128
        assert row["markdown_output"].endswith("[dashboard payload truncated]")

    def test_finish_review_retries_database_lock(self, storage, monkeypatch):
        import sqlite3

        request_id = storage.create_review(repo_name="r", pr_number=5, pr_url="u5")
        original_connect = storage._connect
        calls = {"count": 0}

        def flaky_connect(timeout_seconds=10):
            calls["count"] += 1
            if calls["count"] <= 2:
                raise sqlite3.OperationalError("database is locked")
            return original_connect(timeout_seconds)

        monkeypatch.setattr(storage, "_connect", flaky_connect)
        monkeypatch.setattr("pr_agent.dashboard.storage.time.sleep", lambda _: None)
        storage.finish_review(request_id, [], verdict="APPROVE")

        assert calls["count"] == 3
        assert storage.get_review_by_request_id(request_id)["status"] == "COMPLETED"

    def test_audit_writes_use_short_database_timeout(self, storage, monkeypatch):
        timeouts = []
        original_connect = storage._connect

        def capture_timeout(timeout_seconds=10):
            timeouts.append(timeout_seconds)
            return original_connect(timeout_seconds)

        monkeypatch.setattr(storage, "_connect", capture_timeout)
        assert storage.create_review(repo_name="r", pr_number=6, pr_url="u6")
        assert timeouts
        assert max(timeouts) <= 0.5

    def test_list_reviews_filters(self, storage):
        _seed_review(storage, repo="a/b", pr=1, verdict="APPROVE")
        _seed_review(storage, repo="c/d", pr=2, verdict="REQUEST_CHANGES",
                     severities=("P1", "P2"))
        assert storage.list_reviews(repo="a/b")["total"] == 1
        assert storage.list_reviews(verdict="REQUEST_CHANGES")["total"] == 1
        assert storage.list_reviews()["total"] == 2

    def test_list_reviews_severity_counts(self, storage):
        _seed_review(storage, repo="a/b", pr=1, severities=("P0", "P1", "P1"))
        items = storage.list_reviews(repo="a/b")["items"]
        assert items[0]["severity_counts"] == {"P0": 1, "P1": 2}

    def test_list_reviews_uses_id_to_break_timestamp_ties(self, storage):
        request_ids = [_seed_review(storage, pr=number) for number in range(1, 4)]
        for request_id in request_ids:
            storage._write(
                "UPDATE reviews SET created_at = ? WHERE request_id = ?",
                ("2026-01-01 00:00:00", request_id))

        first_page = storage.list_reviews(limit=2, offset=0)["items"]
        second_page = storage.list_reviews(limit=2, offset=2)["items"]

        assert [row["request_id"] for row in first_page] == list(reversed(request_ids[1:]))
        assert [row["request_id"] for row in second_page] == [request_ids[0]]

    def test_get_review_detail_contains_issues(self, storage):
        request_id = _seed_review(storage)
        detail = storage.get_review_detail(storage.get_review_by_request_id(request_id)["id"])
        assert detail["pr_title"] == "Test PR"
        assert len(detail["issues"]) == 1
        assert detail["issues"][0]["severity"] == "P0"
        assert detail["markdown_output"] == "# report"
        assert storage.get_review_detail(999999) is None

    def test_list_repos_aggregates(self, storage):
        _seed_review(storage, repo="x/y", pr=1)
        _seed_review(storage, repo="x/y", pr=2)
        _seed_review(storage, repo="z/w", pr=1)
        repos = {r["repo_name"]: r["review_count"] for r in storage.list_repos()}
        assert repos == {"x/y": 2, "z/w": 1}


class TestStats:
    def test_empty_database_uses_zero_for_numeric_aggregates(self, storage):
        stats = storage.stats_overview()
        for field in ("total", "today", "failed", "running", "prompt_tokens",
                      "completion_tokens", "total_tokens", "p0_p1_blocked"):
            assert stats[field] == 0

    def test_stats_overview_shape(self, storage):
        _seed_review(storage, repo="a/b", pr=1, severities=("P0", "P1"))
        _seed_review(storage, repo="a/b", pr=2, severities=())
        stats = storage.stats_overview()
        assert stats["total"] == 2
        assert stats["failed"] == 0
        assert stats["p0_p1_blocked"] == 2
        assert stats["severity_distribution"] == {"P0": 1, "P1": 1}
        assert stats["total_tokens"] == 400
        assert "daily_trend" in stats

    def test_stats_running_counted(self, storage):
        _seed_review(storage, status_complete=False)
        stats = storage.stats_overview()
        assert stats["running"] == 1

    def test_stats_skipped_not_running(self, storage):
        request_id = storage.create_review(repo_name="r", pr_number=9, pr_url="u9")
        storage.skip_review(request_id, "no files")
        stats = storage.stats_overview()
        assert stats["running"] == 0

    def test_initialize_expires_stale_running_reviews(self, storage):
        stale = storage.create_review(repo_name="r", pr_number=10, pr_url="u10")
        recent = storage.create_review(repo_name="r", pr_number=11, pr_url="u11")
        stale_time = storage_module._utc_at(
            time.time() - storage_module.STALE_REVIEW_SECONDS - 60)
        storage._write("UPDATE reviews SET created_at = ? WHERE request_id = ?",
                       (stale_time, stale))

        storage.initialize()

        stale_row = storage.get_review_by_request_id(stale)
        assert stale_row["status"] == "FAILED"
        assert "未正常结束" in stale_row["error_message"]
        assert storage.get_review_by_request_id(recent)["status"] == "RUNNING"

    def test_stats_periodically_reconcile_stale_reviews(self, storage):
        request_id = storage.create_review(repo_name="r", pr_number=12, pr_url="u12")
        stale_time = storage_module._utc_at(
            time.time() - storage_module.STALE_REVIEW_SECONDS - 60)
        storage._write("UPDATE reviews SET created_at = ? WHERE request_id = ?",
                       (stale_time, request_id))
        storage._last_stale_cleanup = time.monotonic() - STALE_CLEANUP_INTERVAL_SECONDS - 1
        previous_cleanup = storage._last_stale_cleanup

        stats = storage.stats_overview()

        assert stats["running"] == 0
        assert storage._last_stale_cleanup > previous_cleanup
        assert storage.get_review_by_request_id(request_id)["status"] == "FAILED"

    def test_periodic_maintenance_caps_review_count(self, storage, monkeypatch):
        monkeypatch.setattr(storage_module, "MAX_REVIEW_RECORDS", 3)
        for number in range(5):
            request_id = storage.create_review(repo_name="r", pr_number=number, pr_url=f"u{number}")
            storage.complete_review(request_id)
        storage.reconcile_stale_reviews(force=True)
        assert storage.list_reviews()["total"] == 3

    def test_periodic_maintenance_preserves_running_reviews(self, storage, monkeypatch):
        monkeypatch.setattr(storage_module, "MAX_REVIEW_RECORDS", 3)
        running = storage.create_review(repo_name="r", pr_number=1, pr_url="running")
        for number in range(4):
            request_id = storage.create_review(repo_name="r", pr_number=number + 2, pr_url=f"done-{number}")
            storage.complete_review(request_id)

        storage.reconcile_stale_reviews(force=True)

        assert storage.get_review_by_request_id(running)["status"] == "RUNNING"
        assert storage.list_reviews()["total"] == 3

    def test_periodic_maintenance_allows_running_reviews_to_exceed_cap(self, storage, monkeypatch):
        monkeypatch.setattr(storage_module, "MAX_REVIEW_RECORDS", 3)
        completed = storage.create_review(repo_name="r", pr_number=1, pr_url="completed")
        storage.complete_review(completed)
        running = [
            storage.create_review(repo_name="r", pr_number=number, pr_url=f"running-{number}")
            for number in range(2, 6)
        ]

        storage.reconcile_stale_reviews(force=True)

        assert storage.get_review_by_request_id(completed) is None
        assert storage.list_reviews()["total"] == 4
        assert all(storage.get_review_by_request_id(request_id)["status"] == "RUNNING"
                   for request_id in running)

    def test_periodic_maintenance_deletes_expired_history(self, storage):
        request_id = storage.create_review(repo_name="r", pr_number=20, pr_url="u20")
        storage._write("UPDATE reviews SET created_at = ? WHERE request_id = ?",
                       ("2000-01-01 00:00:00", request_id))
        storage.reconcile_stale_reviews(force=True)
        assert storage.get_review_by_request_id(request_id) is None


class TestAuditLogs:
    def test_add_and_list(self, storage):
        storage.add_audit_log("UPDATE_CONFIG", {"fields": ["model"]}, ip_address="1.2.3.4")
        logs = storage.list_audit_logs()
        assert logs[0]["action"] == "UPDATE_CONFIG"
        import json
        assert json.loads(logs[0]["details_json"]) == {"fields": ["model"]}

    def test_limit(self, storage):
        for i in range(5):
            storage.add_audit_log(f"A{i}")
        assert len(storage.list_audit_logs(limit=3)) == 3


class TestSharedAuthState:
    def test_session_is_shared_across_storage_instances(self, storage):
        other = DashboardStorage(db_path=storage.db_path)
        assert storage.create_session("token-hash", 4_000_000_000)
        assert other.session_is_valid("token-hash", now=1_000_000_000)
        other.revoke_session("token-hash")
        assert not storage.session_is_valid("token-hash", now=1_000_000_000)

    def test_login_attempts_are_shared_and_bounded(self, storage):
        other = DashboardStorage(db_path=storage.db_path)
        for i in range(20):
            storage.verify_login_attempt(
                f"key-{i}", False, attempted_at=1000 + i,
                window_seconds=1000, max_attempts=5, max_rows=10)
        assert other.login_attempt_row_count() == 10
        decision = other.verify_login_attempt(
            "key-19", False, attempted_at=1020, window_seconds=1000,
            max_attempts=5, max_rows=10)
        assert decision["failed_count"] == 2

    def test_password_rotation_purges_sessions_before_old_password_returns(self, storage):
        assert storage.sync_admin_password("password-a")
        assert storage.create_session("session-a", 4_000_000_000)
        assert storage.session_is_valid("session-a", now=1_000_000_000)

        assert storage.sync_admin_password("password-b")
        assert storage.sync_admin_password("password-a")

        assert not storage.session_is_valid("session-a", now=1_000_000_000)

    def test_same_password_is_stable_across_workers_and_worker_restart(self, storage):
        assert storage.sync_admin_password("shared-password")
        assert storage.create_session("shared-session", 4_000_000_000)
        worker = DashboardStorage(db_path=storage.db_path)
        assert worker.sync_admin_password("shared-password")
        worker.initialize()
        assert worker.session_is_valid("shared-session", now=1_000_000_000)

    def test_password_fingerprint_salt_is_unique_per_installation(self, tmp_path):
        first = DashboardStorage(db_path=str(tmp_path / "one" / "review.db"))
        second = DashboardStorage(db_path=str(tmp_path / "two" / "review.db"))
        first.initialize()
        second.initialize()
        assert first.sync_admin_password("same-password")
        assert second.sync_admin_password("same-password")
        first_state = first._read("SELECT * FROM dashboard_auth_state WHERE id = 1")[0]
        second_state = second._read("SELECT * FROM dashboard_auth_state WHERE id = 1")[0]
        assert first_state["fingerprint_salt"] != second_state["fingerprint_salt"]
        assert first_state["password_fingerprint"] != second_state["password_fingerprint"]

    def test_concurrent_workers_cannot_exceed_lockout_threshold(self, storage):
        from concurrent.futures import ThreadPoolExecutor

        def wrong_attempt(_):
            worker = DashboardStorage(db_path=storage.db_path)
            return worker.verify_login_attempt(
                "same-key", False, attempted_at=1000, window_seconds=1000,
                max_attempts=5, max_rows=100)

        with ThreadPoolExecutor(max_workers=10) as pool:
            decisions = list(pool.map(wrong_attempt, range(10)))

        assert sum(not item["locked_out"] for item in decisions) == 5
        final = DashboardStorage(db_path=storage.db_path).verify_login_attempt(
            "same-key", True, attempted_at=1001, window_seconds=1000,
            max_attempts=5, max_rows=100)
        assert final["locked_out"] is True
        assert final["authenticated"] is False


class TestStoragePermissions:
    def test_new_database_directory_and_sidecars_are_owner_only(self, tmp_path):
        directory = tmp_path / "dedicated-dashboard-data"
        storage = DashboardStorage(db_path=str(directory / "review.db"))
        storage.initialize()
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(storage.db_path).st_mode) == 0o600

        for suffix in ("-wal", "-shm"):
            path = f"{storage.db_path}{suffix}"
            with open(path, "a", encoding="utf-8"):
                pass
        storage._protect_storage_permissions()
        assert stat.S_IMODE(os.stat(f"{storage.db_path}-wal").st_mode) == 0o600
        assert stat.S_IMODE(os.stat(f"{storage.db_path}-shm").st_mode) == 0o600

    def test_existing_parent_directory_permissions_are_not_changed(self, tmp_path, monkeypatch):
        directory = tmp_path / "shared-parent"
        directory.mkdir()
        real_chmod = os.chmod
        chmod_targets = []

        def record_chmod(path, mode):
            chmod_targets.append(os.fspath(path))
            real_chmod(path, mode)

        monkeypatch.setattr("pr_agent.dashboard.storage.os.chmod", record_chmod)
        DashboardStorage(db_path=str(directory / "review.db")).initialize()

        assert str(directory) not in chmod_targets

    def test_vanished_sqlite_sidecar_does_not_fail_permission_protection(self, storage, monkeypatch):
        real_chmod = os.chmod

        def disappearing_sidecar(path, mode):
            if os.fspath(path).endswith("-wal"):
                raise FileNotFoundError(path)
            real_chmod(path, mode)

        monkeypatch.setattr("pr_agent.dashboard.storage.os.chmod", disappearing_sidecar)

        storage._protect_storage_permissions()
        assert stat.S_IMODE(os.stat(storage.db_path).st_mode) == 0o600


class TestHealth:
    def test_health_ok(self, storage):
        health = storage.health()
        assert health["ok"] is True
        assert health["latency_ms"] >= 0
        assert health["size_bytes"] >= 0

    def test_health_bad_path(self, tmp_path):
        broken = DashboardStorage(db_path=str(tmp_path / "nonexistent_dir" / "x.db"))
        health = broken.health()
        assert health["ok"] is False


class TestFailSafe:
    def test_write_failure_is_swallowed(self, storage, tmp_path):
        """A broken database must never raise out of a write entry point."""
        # point the db at an unusable path: connect() itself will fail
        storage.db_path = str(tmp_path / "no_dir" / "sub" / "x.db")
        storage.complete_review("whatever", verdict="APPROVE")  # must not raise
        storage.fail_review("whatever", "err")
        storage.add_audit_log("X")
        storage.add_review_issues("whatever", [{"severity": "P0"}])


def test_singleton_retries_initialization_after_transient_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module, "_storage", None)
    monkeypatch.setattr(storage_module, "DEFAULT_DB_PATH", str(tmp_path / "retry.db"))
    original_initialize = DashboardStorage.initialize
    calls = {"count": 0}

    def flaky_initialize(self):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("mount unavailable")
        return original_initialize(self)

    monkeypatch.setattr(DashboardStorage, "initialize", flaky_initialize)
    first = storage_module.get_storage()
    second = storage_module.get_storage()

    assert first is not second
    assert calls["count"] == 2
    assert storage_module._storage is second
