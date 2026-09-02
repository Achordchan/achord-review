"""Tests for dashboard operation execution without process-local task state."""

import subprocess

from pr_agent.dashboard import ops


def test_git_pull_returns_completed_output(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_DIR", str(tmp_path))
    monkeypatch.setattr(
        ops.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="Updating files\nDone\n"))

    result = ops.git_pull()

    assert result == {
        "started": True, "completed": True, "exit_code": 0,
        "output": ["Updating files", "Done"],
    }


def test_git_pull_reports_not_started_without_binary(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(ops.subprocess, "run", missing)

    result = ops.git_pull()

    assert result["started"] is False
    assert result["completed"] is True
    assert "task_id" not in result


def test_restart_stops_after_failed_preflight(monkeypatch):
    monkeypatch.setattr(
        ops.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=1, stdout="daemon unavailable"))
    popen_called = False

    def unexpected_popen(*args, **kwargs):
        nonlocal popen_called
        popen_called = True

    monkeypatch.setattr(ops.subprocess, "Popen", unexpected_popen)

    result = ops.restart_container()

    assert result["started"] is False
    assert popen_called is False
