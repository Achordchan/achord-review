"""Tests for dashboard operation execution without process-local task state."""

import asyncio
import subprocess
import sys

import pytest

from pr_agent.dashboard import ops


@pytest.fixture(autouse=True)
def _isolated_ops_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "OPS_LOCK_PATH", str(tmp_path / "dashboard-ops.lock"))


def test_git_pull_returns_completed_output(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_DIR", str(tmp_path))
    monkeypatch.setattr(
        ops, "_run_bounded_command",
        lambda *args, **kwargs: {
            "started": True, "completed": True, "exit_code": 0,
            "timed_out": False, "output": ["Updating files", "Done"],
        })

    result = ops.git_pull()

    assert result == {
        "started": True, "completed": True, "exit_code": 0,
        "timed_out": False, "output": ["Updating files", "Done"],
    }


def test_git_pull_reports_not_started_without_binary(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(ops, "_run_bounded_command", missing)

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


def test_tail_logs_caps_a_single_huge_line(monkeypatch, tmp_path):
    log_path = tmp_path / "service.log"
    log_path.write_bytes(b"x" * (ops.MAX_LOG_TAIL_BYTES + 1024))
    monkeypatch.setenv("ACHORD_REVIEW_LOG_FILE", str(log_path))

    lines = ops.tail_logs()

    assert len(lines) == 1
    assert len(lines[0].encode()) == ops.MAX_LOG_TAIL_BYTES


def test_bounded_command_keeps_only_output_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "MAX_GIT_OUTPUT_BYTES", 1024)
    result = ops._run_bounded_command(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 8192)"],
        cwd=str(tmp_path), timeout_seconds=10)
    assert result["exit_code"] == 0
    assert len("\n".join(result["output"]).encode()) <= 1024


def test_operations_reject_while_another_worker_holds_lock(monkeypatch):
    called = False

    def unexpected(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(ops, "_run_bounded_command", unexpected)
    with ops._operation_lock() as lock_file:
        assert lock_file is not None
        result = ops.git_pull()
    assert result["started"] is False
    assert called is False


def test_llm_probe_uses_configured_adapter(monkeypatch):
    captured = {}

    class FakeHandler:
        async def chat_completion(self, **kwargs):
            captured.update(kwargs)
            return "pong", "stop"

    monkeypatch.setattr(ops, "_get_probe_ai_handler", lambda: FakeHandler())
    result = ops.probe_llm()
    assert result["ok"] is True
    assert captured["model"]
    assert captured["user"] == "Reply with exactly: pong"


def test_llm_probe_enforces_its_own_timeout(monkeypatch):
    class SlowHandler:
        async def chat_completion(self, **kwargs):
            await asyncio.Event().wait()

    monkeypatch.setattr(ops, "_get_probe_ai_handler", lambda: SlowHandler())

    result = ops.probe_llm(timeout_seconds=0.01)

    assert result["ok"] is False
    assert result["error"] == "LLM probe timed out after 0.01 seconds"
