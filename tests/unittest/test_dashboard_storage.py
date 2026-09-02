"""Tests for the dashboard storage layer (pr_agent/dashboard/storage.py)."""

import os

import pytest

from pr_agent.dashboard.storage import DashboardStorage


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
