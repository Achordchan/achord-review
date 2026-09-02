"""Tests for dashboard operation execution without process-local task state."""

import asyncio
import io
import signal
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


def test_restart_reports_immediate_command_rejection(monkeypatch):
    monkeypatch.setattr(
        ops.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="container found"))

    class RejectedProcess:
        def poll(self):
            return 13

    monkeypatch.setattr(ops.subprocess, "Popen", lambda *args, **kwargs: RejectedProcess())
    monkeypatch.setattr(ops.time, "sleep", lambda _: None)

    result = ops.restart_container()

    assert result["started"] is False
    assert result["completed"] is True
    assert "退出码 13" in result["output"][0]


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


def test_bounded_command_kills_process_group_on_timeout(tmp_path, monkeypatch):
    class FakeProcess:
        pid = 4242
        stdout = io.BytesIO(b"")

        def __init__(self):
            self.wait_calls = 0

        def wait(self, timeout):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise subprocess.TimeoutExpired(cmd="git pull", timeout=timeout)
            return -signal.SIGKILL

    process = FakeProcess()
    popen_kwargs = {}
    killed = []

    def fake_popen(*args, **kwargs):
        popen_kwargs.update(kwargs)
        return process

    monkeypatch.setattr(ops.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ops.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    result = ops._run_bounded_command(
        ["git", "pull"], cwd=str(tmp_path), timeout_seconds=1)

    assert popen_kwargs["start_new_session"] is True
    assert killed == [(process.pid, signal.SIGKILL)]
    assert result["timed_out"] is True


def test_bounded_command_reports_when_process_survives_kill_wait(tmp_path, monkeypatch):
    class StuckProcess:
        pid = 4243
        stdout = io.BytesIO(b"")

        def wait(self, timeout):
            raise subprocess.TimeoutExpired(cmd="git pull", timeout=timeout)

    process = StuckProcess()
    retained = []
    monkeypatch.setattr(ops.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(ops.os, "killpg", lambda *args: None)
    monkeypatch.setattr(
        ops, "_retain_operation_lock",
        lambda lock_file, proc: retained.append((lock_file, proc)) or True)

    result = ops._run_bounded_command(
        ["git", "pull"], cwd=str(tmp_path), timeout_seconds=1, lock_file="lock")

    assert result["timed_out"] is True
    assert result["completed"] is False
    assert result["exit_code"] is None
    assert result["lock_retained"] is True
    assert retained == [("lock", process)]
    assert "运维锁将保持到进程退出" in result["output"][-1]


def test_retained_lock_blocks_operations_until_process_exits(monkeypatch):
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.2)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    with ops._operation_lock() as lock_file:
        assert lock_file is not None
        assert ops._retain_operation_lock(lock_file, process) is True

    with ops._operation_lock() as blocked_lock:
        assert blocked_lock is None
    process.wait(timeout=2)
    for _ in range(20):
        with ops._operation_lock() as released_lock:
            if released_lock is not None:
                break
        __import__("time").sleep(0.01)
    assert released_lock is not None


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


def test_github_probe_mints_and_validates_installation_token(monkeypatch):
    import jwt
    import requests

    class FakeSettings:
        values = {"github.app_id": "123", "github.private_key": "private-key"}

        def get(self, key, default=None):
            return self.values.get(key, default)

    class Response:
        def __init__(self, status_code, payload, headers=None):
            self.status_code = status_code
            self.payload = payload
            self.headers = headers or {}

        def json(self):
            return self.payload

    import pr_agent.config_loader as config_loader
    monkeypatch.setattr(config_loader, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(jwt, "encode", lambda *args, **kwargs: "app-jwt")
    requested = []

    def fake_get(url, **kwargs):
        requested.append(("GET", url, kwargs["headers"]["Authorization"]))
        if url.endswith("/app"):
            return Response(200, {"name": "achord-review"})
        if "/app/installations" in url:
            if "page=2" in url:
                return Response(200, [{"id": 42, "suspended_at": None}])
            return Response(
                200,
                [{"id": index, "suspended_at": "2026-01-01"} for index in range(100)],
                {"Link": '<https://api.github.com/app/installations?per_page=100&page=2>; rel="next"'},
            )
        return Response(200, {"total_count": 7})

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(
        requests, "post",
        lambda url, **kwargs: Response(201, {"token": "installation-token"}))
    monkeypatch.setattr(
        requests, "delete",
        lambda url, **kwargs: requested.append(("DELETE", url, kwargs["headers"]["Authorization"])))

    result = ops.probe_github_app()

    assert result == {
        "ok": True,
        "app_id": "123",
        "app_name": "achord-review",
        "installation_id": 42,
        "repository_count": 7,
    }
    assert ("GET", "https://api.github.com/installation/repositories?per_page=1",
            "Bearer installation-token") in requested
    assert any("/app/installations?per_page=100&page=2" in url for method, url, _ in requested)
    assert ("DELETE", "https://api.github.com/installation/token",
            "Bearer installation-token") in requested
