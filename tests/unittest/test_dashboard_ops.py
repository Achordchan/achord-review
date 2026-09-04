"""Tests for dashboard operation execution without process-local task state."""

import asyncio
import io
import os
import signal
import subprocess
import sys

import pytest

from pr_agent.dashboard import ops


@pytest.fixture(autouse=True)
def _isolated_ops_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "OPS_LOCK_PATH", str(tmp_path / "dashboard-ops.lock"))
    # Staged releases refuse to run without the persistent config directory.
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr(ops, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(ops, "SOURCE_DIR", "")


def test_git_pull_returns_completed_output(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_DIR", str(tmp_path))
    releases = tmp_path / "releases"
    releases.mkdir()
    monkeypatch.setattr(ops, "RELEASES_DIR", str(releases))
    monkeypatch.setattr(
        ops, "git_pull_capability", lambda: {"available": True, "reason": "ready"})
    monkeypatch.setattr(ops, "rebuild_required", lambda: False)
    revision = "a" * 40
    monkeypatch.setattr(
        ops, "_git_text",
        lambda args, timeout: (0, "") if args == ["fetch", "--quiet"] else (0, revision))
    monkeypatch.setattr(
        ops, "_run_bounded_command",
        lambda *args, **kwargs: {
            "started": True, "completed": True, "exit_code": 0,
            "timed_out": False, "output": ["Updating files", "Done"],
        })

    result = ops.git_pull()

    assert result["started"] is True
    assert result["completed"] is True
    assert result["release"] == revision
    assert result["mode"] == "staged"
    assert result["dependencies_changed"] is False
    assert "运行中的后端与静态资源尚未改变" in result["output"][-1]
    assert (releases / ".pending-release").read_text().strip() == str(releases / revision)


def test_git_pull_reports_not_started_without_binary(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(ops, "_run_bounded_command", missing)
    monkeypatch.setattr(
        ops, "git_pull_capability", lambda: {"available": True, "reason": "ready"})
    monkeypatch.setattr(ops, "_git_text", lambda *args: (0, "") if args[0] == ["fetch", "--quiet"] else (0, "a" * 40))

    result = ops.git_pull()

    assert result["started"] is False
    assert result["completed"] is True
    assert "task_id" not in result


def test_git_pull_is_disabled_without_deliberate_checkout(monkeypatch):
    monkeypatch.setattr(ops, "REPO_DIR", "")
    result = ops.git_pull_capability()

    assert result["available"] is False
    assert "宿主机" in result["reason"]


def test_git_pull_capability_verifies_real_checkout(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_DIR", str(tmp_path))
    monkeypatch.setattr(ops, "RELEASES_DIR", str(tmp_path / "releases"))
    monkeypatch.setattr(
        ops.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="true\n"))

    assert ops.git_pull_capability()["available"] is True


def test_restart_stops_after_failed_preflight(monkeypatch):
    monkeypatch.setattr(
        ops.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=1, stdout="daemon unavailable"))
    command_called = False

    def unexpected_command(*args, **kwargs):
        nonlocal command_called
        command_called = True

    monkeypatch.setattr(ops, "_run_bounded_command", unexpected_command)

    result, ticket = ops.prepare_restart()

    assert result["started"] is False
    assert ticket is None
    assert command_called is False


def test_restart_reports_scheduler_lock_conflict(monkeypatch):
    monkeypatch.setattr(
        ops, "_operation_lock",
        lambda: __import__("contextlib").nullcontext(None))

    result, ticket = ops.prepare_restart()

    assert result["started"] is False
    assert result["completed"] is True
    assert ticket is None
    assert "正在执行" in result["output"][0]


def test_restart_prepares_ticket_without_running_command(monkeypatch):
    monkeypatch.setattr(
        ops.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="c" * 64 + "\n"))
    monkeypatch.setattr(ops, "_own_container_ids", lambda: {"c" * 12})
    commands = []
    monkeypatch.setattr(
        ops, "_run_bounded_command", lambda *args, **kwargs: commands.append(args))

    result, ticket = ops.prepare_restart()

    assert result["started"] is True
    assert result["completed"] is False
    assert result["scheduled"] is True
    assert ticket is not None
    assert commands == []
    ticket.lock_context.__exit__(None, None, None)


def test_execute_restart_runs_fixed_bounded_command_and_releases_ticket(monkeypatch):
    class LockContext:
        exited = False

        def __exit__(self, *args):
            self.exited = True

    lock_context = LockContext()
    ticket = ops._RestartTicket(lock_context, "lock-file")
    captured = {}
    monkeypatch.setattr(
        ops, "_run_bounded_command",
        lambda *args, **kwargs: captured.update({"args": args, "kwargs": kwargs}) or {
            "exit_code": 0,
        })

    ops.execute_restart(ticket)

    assert captured["args"][0] == [
        "docker", "restart", "--timeout", "30", ops.CONTAINER_NAME]
    assert captured["kwargs"]["timeout_seconds"] == ops.RESTART_COMMAND_TIMEOUT_SECONDS
    assert captured["kwargs"]["lock_file"] == "lock-file"
    assert lock_context.exited is True


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
    monkeypatch.setattr(
        ops, "git_pull_capability", lambda: {"available": True, "reason": "ready"})
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
        lambda url, **kwargs: requested.append(
            ("DELETE", url, kwargs["headers"]["Authorization"])) or Response(204, None))

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


def test_github_probe_skips_installations_without_repository_access(monkeypatch):
    import jwt
    import requests

    class FakeSettings:
        def get(self, key, default=None):
            return {
                "github.app_id": "123",
                "github.private_key": "private-key",
            }.get(key, default)

    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.payload = payload
            self.headers = {}

        def json(self):
            return self.payload

    import pr_agent.config_loader as config_loader
    monkeypatch.setattr(config_loader, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(jwt, "encode", lambda *args, **kwargs: "app-jwt")
    repository_counts = {1: 0, 2: 3}
    revoked = []

    def fake_get(url, **kwargs):
        if url.endswith("/app"):
            return Response(200, {"name": "achord-review"})
        if "/app/installations" in url:
            return Response(200, [
                {"id": 1, "suspended_at": None},
                {"id": 2, "suspended_at": None},
            ])
        token = kwargs["headers"]["Authorization"].removeprefix("Bearer token-")
        return Response(200, {"total_count": repository_counts[int(token)]})

    def fake_post(url, **kwargs):
        installation_id = int(url.split("/installations/", 1)[1].split("/", 1)[0])
        return Response(201, {"token": f"token-{installation_id}"})

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(
        requests, "delete",
        lambda url, **kwargs: revoked.append(kwargs["headers"]["Authorization"]))

    result = ops.probe_github_app()

    assert result["ok"] is True
    assert result["installation_id"] == 2
    assert result["repository_count"] == 3
    assert revoked == ["Bearer token-1", "Bearer token-2"]

    repository_counts[2] = 0
    result = ops.probe_github_app()
    assert result["ok"] is False
    assert result["installation_count"] == 2
    assert "no accessible repositories" in result["error"]


def test_github_probe_enforces_one_deadline_across_installations(monkeypatch):
    import jwt
    import requests

    class FakeSettings:
        def get(self, key, default=None):
            return {
                "github.app_id": "123",
                "github.private_key": "private-key",
            }.get(key, default)

    class Response:
        headers = {}

        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.payload = payload

        def json(self):
            return self.payload

    import pr_agent.config_loader as config_loader
    monkeypatch.setattr(config_loader, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(jwt, "encode", lambda *args, **kwargs: "app-jwt")
    clock = {"now": 0.0}
    posts = []

    def fake_get(url, **kwargs):
        if url.endswith("/app"):
            return Response(200, {"name": "achord-review"})
        clock["now"] = 2.0
        return Response(200, [{"id": 1, "suspended_at": None}])

    monkeypatch.setattr(ops.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: posts.append(args) or None)

    result = ops.probe_github_app(timeout_seconds=1)

    assert result["ok"] is False
    assert result["error"] == "GitHub App probe exceeded its 1s deadline"
    assert posts == []


def test_github_probe_revokes_its_token_even_with_the_deadline_spent(monkeypatch):
    # Revocation used to reuse the probe's remaining budget, so a probe that
    # spent it left the temporary installation token live until it expired.
    import jwt
    import requests

    class FakeSettings:
        def get(self, key, default=None):
            return {
                "github.app_id": "123",
                "github.private_key": "private-key",
            }.get(key, default)

    class Response:
        headers = {}

        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.payload = payload

        def json(self):
            return self.payload

    import pr_agent.config_loader as config_loader
    monkeypatch.setattr(config_loader, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(jwt, "encode", lambda *args, **kwargs: "app-jwt")
    clock = {"now": 0.0}
    deletes = []

    def fake_get(url, **kwargs):
        if url.endswith("/app"):
            return Response(200, {"name": "achord-review"})
        if "/app/installations" in url:
            return Response(200, [{"id": 1, "suspended_at": None}])
        # The repository check consumes everything that was left of the budget.
        clock["now"] = 30.0
        return Response(200, {"total_count": 3})

    monkeypatch.setattr(ops.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(
        requests, "post",
        lambda url, **kwargs: Response(201, {"token": "installation-token"}))
    monkeypatch.setattr(
        requests, "delete",
        lambda url, **kwargs: deletes.append((url, kwargs["timeout"])) or Response(204, None))

    result = ops.probe_github_app(timeout_seconds=10)

    assert result["ok"] is True
    assert deletes == [("https://api.github.com/installation/token",
                        ops.TOKEN_REVOCATION_TIMEOUT_SECONDS)]


def test_github_probe_reports_a_rejected_token_revocation(monkeypatch):
    # A 403/429/500 leaves the repository-access token live for up to an hour,
    # so a probe that reports success must not stay silent about it.
    import jwt
    import requests

    class FakeSettings:
        def get(self, key, default=None):
            return {
                "github.app_id": "123",
                "github.private_key": "private-key",
            }.get(key, default)

    class Response:
        headers = {}

        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.payload = payload

        def json(self):
            return self.payload

    class RecordingLogger:
        def __init__(self, warnings):
            self.warnings = warnings

        def warning(self, message):
            self.warnings.append(message)

        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    warnings = []
    import pr_agent.config_loader as config_loader
    monkeypatch.setattr(config_loader, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(jwt, "encode", lambda *args, **kwargs: "app-jwt")
    monkeypatch.setattr(ops, "get_logger", lambda: RecordingLogger(warnings))

    def fake_get(url, **kwargs):
        if url.endswith("/app"):
            return Response(200, {"name": "achord-review"})
        if "/app/installations" in url:
            return Response(200, [{"id": 1, "suspended_at": None}])
        return Response(200, {"total_count": 3})

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(
        requests, "post",
        lambda url, **kwargs: Response(201, {"token": "installation-token"}))
    monkeypatch.setattr(requests, "delete", lambda url, **kwargs: Response(403, None))

    result = ops.probe_github_app(timeout_seconds=10)

    assert result["ok"] is True
    assert any("403" in message for message in warnings)


def _fake_git_text(responses):
    """Map a git argument tuple to a (returncode, stdout) pair for check_update."""
    def _run(args, timeout_seconds):
        return responses[tuple(args)]
    return _run


def test_check_update_flags_an_available_update(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_DIR", str(tmp_path))
    monkeypatch.setattr(
        ops, "git_pull_capability", lambda: {"available": True, "reason": "ready"})
    monkeypatch.setattr(ops, "_git_text", _fake_git_text({
        ("rev-parse", "--short", "HEAD"): (0, "aaaaaaa"),
        ("log", "-1", "--format=%s"): (0, "old commit"),
        ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"): (0, "origin/main"),
        ("fetch", "--quiet", "origin"): (0, ""),
        ("rev-parse", "--short", "origin/main"): (0, "bbbbbbb"),
        ("log", "-1", "--format=%s", "origin/main"): (0, "new commit"),
        ("rev-list", "--count", "HEAD..origin/main"): (0, "3"),
        ("rev-list", "--count", "origin/main..HEAD"): (0, "0"),
    }))

    result = ops.check_update()

    assert result["checked"] is True
    assert result["update_available"] is True
    assert result["behind"] == 3
    assert result["current"]["sha"] == "aaaaaaa"
    assert result["latest"]["sha"] == "bbbbbbb"
    assert result["latest"]["branch"] == "origin/main"


def test_check_update_reports_up_to_date(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_DIR", str(tmp_path))
    monkeypatch.setattr(
        ops, "git_pull_capability", lambda: {"available": True, "reason": "ready"})
    monkeypatch.setattr(ops, "_git_text", _fake_git_text({
        ("rev-parse", "--short", "HEAD"): (0, "aaaaaaa"),
        ("log", "-1", "--format=%s"): (0, "head"),
        ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"): (0, "origin/main"),
        ("fetch", "--quiet", "origin"): (0, ""),
        ("rev-parse", "--short", "origin/main"): (0, "aaaaaaa"),
        ("log", "-1", "--format=%s", "origin/main"): (0, "head"),
        ("rev-list", "--count", "HEAD..origin/main"): (0, "0"),
        ("rev-list", "--count", "origin/main..HEAD"): (0, "0"),
    }))

    result = ops.check_update()

    assert result["checked"] is True
    assert result["update_available"] is False
    assert result["behind"] == 0


def test_check_update_disabled_without_checkout(monkeypatch):
    monkeypatch.setattr(
        ops, "git_pull_capability",
        lambda: {"available": False, "reason": "not a checkout"})

    result = ops.check_update()

    assert result["available"] is False
    assert result["checked"] is False
    assert result["update_available"] is False
    assert result["reason"] == "not a checkout"


def test_check_update_surfaces_fetch_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_DIR", str(tmp_path))
    monkeypatch.setattr(
        ops, "git_pull_capability", lambda: {"available": True, "reason": "ready"})
    monkeypatch.setattr(ops, "_git_text", _fake_git_text({
        ("rev-parse", "--short", "HEAD"): (0, "aaaaaaa"),
        ("log", "-1", "--format=%s"): (0, "head"),
        ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"): (0, "origin/main"),
        ("fetch", "--quiet", "origin"): (1, "fatal: could not read from remote"),
    }))

    result = ops.check_update()

    assert result["checked"] is False
    assert result["update_available"] is False
    assert "could not read from remote" in result["reason"]


def test_restart_capability_prefers_docker_when_endpoint_is_live(monkeypatch):
    container_id = "f" * 64
    monkeypatch.setattr(
        ops.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=container_id + "\n"))
    monkeypatch.setattr(ops, "_own_container_ids", lambda: {container_id[:12]})
    monkeypatch.setattr(ops, "SELF_RESTART_ENABLED", True)

    capability = ops.restart_capability()

    assert capability["available"] is True
    assert capability["mode"] == "docker"


def test_restart_capability_refuses_docker_for_a_mistargeted_container(monkeypatch):
    # The name resolves, but to a different container than the one we run in.
    monkeypatch.setattr(
        ops.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout="a" * 64 + "\n"))
    monkeypatch.setattr(ops, "_own_container_ids", lambda: {"b" * 12})
    monkeypatch.setattr(ops, "SELF_RESTART_ENABLED", False)

    capability = ops.restart_capability()

    assert capability["available"] is False
    assert ops.CONTAINER_NAME in capability["reason"]


def test_restart_capability_falls_back_to_self_when_the_target_is_not_self(monkeypatch):
    monkeypatch.setattr(
        ops.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout="a" * 64 + "\n"))
    monkeypatch.setattr(ops, "_own_container_ids", lambda: {"b" * 12})
    monkeypatch.setattr(ops, "SELF_RESTART_ENABLED", True)

    capability = ops.restart_capability()

    # A mistargeted Docker name must not shadow the safe self-exit path.
    assert capability["available"] is True
    assert capability["mode"] == "self"


def test_restart_capability_falls_back_to_self_exit_without_docker(monkeypatch):
    def no_docker(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(ops.subprocess, "run", no_docker)
    monkeypatch.setattr(ops, "SELF_RESTART_ENABLED", True)

    capability = ops.restart_capability()

    assert capability["available"] is True
    assert capability["mode"] == "self"


def test_restart_capability_stays_disabled_without_docker_or_self_restart(monkeypatch):
    def no_docker(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(ops.subprocess, "run", no_docker)
    monkeypatch.setattr(ops, "SELF_RESTART_ENABLED", False)

    capability = ops.restart_capability()

    assert capability["available"] is False


def test_execute_restart_self_mode_signals_pid_one(monkeypatch):
    signals = []
    monkeypatch.setattr(ops.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    class _Ctx:
        def __exit__(self, *a):
            return False

    ops.execute_restart(ops._RestartTicket(_Ctx(), None, mode="self"))

    assert signals == [(1, ops.signal.SIGTERM)]


def _completed_pull(*args, **kwargs):
    return {"started": True, "completed": True, "exit_code": 0,
            "timed_out": False, "output": ["Updated"]}


def test_git_pull_flags_dependency_changes(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_DIR", str(tmp_path))
    monkeypatch.setattr(ops, "RELEASES_DIR", str(tmp_path / "releases"))
    monkeypatch.setattr(
        ops, "git_pull_capability", lambda: {"available": True, "reason": "ready"})
    monkeypatch.setattr(ops, "_run_bounded_command", _completed_pull)
    monkeypatch.setattr(
        ops, "_git_text",
        lambda args, timeout: (0, "") if args == ["fetch", "--quiet"] else (0, "a" * 40))
    monkeypatch.setattr(ops, "rebuild_required", lambda: True)

    result = ops.git_pull()

    assert result["dependencies_changed"] is True
    assert any("--build" in line for line in result["output"])


def test_git_pull_survives_a_dependency_check_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_DIR", str(tmp_path))
    monkeypatch.setattr(ops, "RELEASES_DIR", str(tmp_path / "releases"))
    monkeypatch.setattr(
        ops, "git_pull_capability", lambda: {"available": True, "reason": "ready"})
    monkeypatch.setattr(ops, "_run_bounded_command", _completed_pull)
    monkeypatch.setattr(
        ops, "_git_text",
        lambda args, timeout: (0, "") if args == ["fetch", "--quiet"] else (0, "a" * 40))

    def _timeout():
        raise subprocess.TimeoutExpired(cmd="git", timeout=5)

    monkeypatch.setattr(ops, "rebuild_required", _timeout)

    result = ops.git_pull()

    # A completed pull is never turned into a failure; the check degrades to
    # a conservative rebuild-required.
    assert result["completed"] is True
    assert result["exit_code"] == 0
    assert result["dependencies_changed"] is True


def test_rebuild_required_compares_baked_and_checkout_fingerprints(monkeypatch, tmp_path):
    baked = tmp_path / "baked"
    checkout = tmp_path / "checkout"
    baked.mkdir()
    checkout.mkdir()
    (baked / "requirements.txt").write_text("requests==1\n")
    (baked / "pyproject.toml").write_text("[project]\n")
    (checkout / "requirements.txt").write_text("requests==1\n")
    (checkout / "pyproject.toml").write_text("[project]\n")
    monkeypatch.setattr(ops, "REPO_DIR", str(checkout))
    monkeypatch.setattr(ops, "DEPS_BAKED_DIR", str(baked))

    assert ops.rebuild_required() is False

    (checkout / "requirements.txt").write_text("requests==2\n")
    assert ops.rebuild_required() is True


def test_rebuild_required_fails_closed_without_a_baked_copy(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_DIR", str(tmp_path))
    monkeypatch.setattr(ops, "DEPS_BAKED_DIR", str(tmp_path / "missing"))
    # An image reused under a mounted checkout without baked fingerprints cannot be
    # verified — fail closed and require a rebuild rather than risk a restart loop.
    assert ops.rebuild_required() is True


def test_rebuild_required_never_blocks_a_non_mounted_deployment(monkeypatch):
    # The standard deployment runs baked code only; a missing baked dir must not
    # start blocking its ordinary restarts.
    monkeypatch.setattr(ops, "REPO_DIR", "")
    monkeypatch.setattr(ops, "DEPS_BAKED_DIR", "/nonexistent")
    assert ops.rebuild_required() is False


def test_prepare_restart_is_blocked_when_a_rebuild_is_required(monkeypatch):
    monkeypatch.setattr(ops, "rebuild_required", lambda: True)
    unexpected = []
    monkeypatch.setattr(ops.subprocess, "run", lambda *a, **k: unexpected.append(a))

    result, ticket = ops.prepare_restart()

    assert result["started"] is False
    assert ticket is None
    assert "重启已被阻止" in result["output"][0]
    assert unexpected == []


def _pending_release_layout(tmp_path, monkeypatch):
    releases = tmp_path / "releases"
    old_release = releases / "old"
    new_release = releases / "new"
    old_release.mkdir(parents=True)
    new_release.mkdir()
    active = releases / "current"
    active.symlink_to(old_release)
    (releases / ".pending-release").write_text(str(new_release) + "\n")
    monkeypatch.setattr(ops, "REPO_DIR", str(active))
    monkeypatch.setattr(ops, "RELEASES_DIR", str(releases))
    monkeypatch.setattr(ops, "rebuild_required", lambda: False)
    monkeypatch.setattr(
        ops, "restart_capability",
        lambda: {"available": True, "mode": "self", "reason": "ready"})
    return releases, old_release, new_release, active


def test_prepare_restart_defers_the_switch_until_restart_initiation(monkeypatch, tmp_path):
    releases, old_release, new_release, active = _pending_release_layout(tmp_path, monkeypatch)

    result, ticket = ops.prepare_restart()

    # The response only announces the switch; `current` still serves the old release.
    assert result["started"] is True
    assert active.resolve() == old_release.resolve()
    assert (releases / ".pending-release").exists()
    assert ticket is not None and ticket.pending == str(new_release.resolve())
    ticket.lock_context.__exit__(None, None, None)


def test_execute_restart_switches_atomically_when_the_signal_is_delivered(monkeypatch, tmp_path):
    releases, _, new_release, active = _pending_release_layout(tmp_path, monkeypatch)
    monkeypatch.setattr(ops.os, "kill", lambda pid, sig: None)
    _, ticket = ops.prepare_restart()

    ops.execute_restart(ticket)

    assert active.resolve() == new_release.resolve()
    assert not (releases / ".pending-release").exists()


def test_execute_restart_rolls_back_when_restart_cannot_be_initiated(monkeypatch, tmp_path):
    releases, old_release, new_release, active = _pending_release_layout(tmp_path, monkeypatch)

    def _refuse(pid, sig):
        raise PermissionError("not PID 1's parent")

    monkeypatch.setattr(ops.os, "kill", _refuse)
    _, ticket = ops.prepare_restart()

    ops.execute_restart(ticket)

    # The old process keeps serving, so `current` and the marker must match it again.
    assert active.resolve() == old_release.resolve()
    assert (releases / ".pending-release").read_text().strip() == str(new_release.resolve())


def test_execute_restart_rolls_back_when_docker_restart_fails(monkeypatch, tmp_path):
    releases, old_release, new_release, active = _pending_release_layout(tmp_path, monkeypatch)
    monkeypatch.setattr(
        ops, "restart_capability",
        lambda: {"available": True, "mode": "docker", "reason": "ready"})
    monkeypatch.setattr(
        ops, "_run_bounded_command",
        lambda *a, **k: {"started": True, "completed": True, "exit_code": 1, "output": ["denied"]})
    _, ticket = ops.prepare_restart()

    ops.execute_restart(ticket)

    assert active.resolve() == old_release.resolve()
    assert (releases / ".pending-release").exists()


def test_git_pull_stages_without_mutating_live_worktree(monkeypatch, tmp_path):
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    writer = tmp_path / "writer"
    releases = tmp_path / "releases"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(source)], check=True, capture_output=True)
    for checkout in (source,):
        subprocess.run(["git", "-C", str(checkout), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Test"], check=True)
    (source / "value.txt").write_text("old\n")
    subprocess.run(["git", "-C", str(source), "add", "value.txt"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-m", "old"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source), "branch", "-M", "main"], check=True)
    subprocess.run(["git", "-C", str(source), "push", "-u", "origin", "main"], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(writer)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(writer), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(writer), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(writer), "checkout", "main"], check=True, capture_output=True)
    (writer / "value.txt").write_text("new\n")
    subprocess.run(["git", "-C", str(writer), "commit", "-am", "new"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(writer), "push"], check=True, capture_output=True)
    releases.mkdir()
    current = releases / "current"
    current.symlink_to(source)
    monkeypatch.setattr(ops, "REPO_DIR", str(current))
    monkeypatch.setattr(ops, "RELEASES_DIR", str(releases))
    monkeypatch.setattr(ops, "UPDATE_REF", "origin/main")
    monkeypatch.setattr(ops, "DEPS_BAKED_DIR", str(source))

    result = ops.git_pull()

    pending = ops._pending_release()
    assert result["completed"] is True
    assert (source / "value.txt").read_text() == "old\n"
    assert pending is not None
    assert (tmp_path / "releases" / result["release"] / "value.txt").read_text() == "new\n"
    settings_link = tmp_path / "releases" / result["release"] / "pr_agent" / "settings_prod"
    assert settings_link.is_symlink() and settings_link.resolve() == (tmp_path / "config").resolve()

    # The prepared release is reported as staged, not re-offered as an update.
    info = ops.check_update()
    assert info["checked"] is True
    assert info["staged"] is True
    assert info["update_available"] is False
    assert info["pending"]["sha"] == result["release"][:7]
    assert info["pending"]["subject"] == "new"


def _staged_repo(tmp_path):
    """A bare remote, a source clone on `main`, and a releases dir with `current`."""
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    releases = tmp_path / "releases"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(source)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
    releases.mkdir()
    return remote, source, releases


def _commit(source, text):
    (source / "value.txt").write_text(text + "\n")
    subprocess.run(["git", "-C", str(source), "add", "value.txt"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", text], check=True, capture_output=True)
    return subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


def _boot_env(monkeypatch, tmp_path, source, releases, build_id):
    # The image bakes no dependency files here, and neither do the committed
    # releases, so every fingerprint is the shared "all missing" digest — a release
    # is provenance-compatible with the image unless a test commits a dependency
    # file that diverges from the baked copy.
    baked = tmp_path / "baked"
    baked.mkdir(exist_ok=True)
    (baked / "build-id").write_text(build_id + "\n")
    monkeypatch.setattr(ops, "DEPS_BAKED_DIR", str(baked))
    monkeypatch.setattr(ops, "SOURCE_DIR", str(source))
    monkeypatch.setattr(ops, "RELEASES_DIR", str(releases))
    monkeypatch.setattr(ops, "REPO_DIR", str(releases / "current"))


def _commit_dep(source, filename, text):
    (source / filename).write_text(text)
    subprocess.run(["git", "-C", str(source), "add", filename], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", f"dep {filename}"],
                   check=True, capture_output=True)
    return subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


def test_git_pull_unregisters_a_failed_worktree_so_the_revision_can_be_retried(monkeypatch, tmp_path):
    _, source, releases = _staged_repo(tmp_path)
    revision = _commit(source, "one")
    (releases / "current").symlink_to(source)
    monkeypatch.setattr(ops, "REPO_DIR", str(releases / "current"))
    monkeypatch.setattr(ops, "RELEASES_DIR", str(releases))
    monkeypatch.setattr(
        ops, "git_pull_capability", lambda: {"available": True, "reason": "ready"})
    monkeypatch.setattr(ops, "rebuild_required", lambda: False)
    monkeypatch.setattr(
        ops, "_git_text",
        lambda args, timeout: (0, "") if args[:1] in (["fetch"], ["merge-base"]) else (0, revision))
    real_run = ops._run_bounded_command

    def _register_then_time_out(argv, cwd, timeout_seconds, lock_file=None):
        # Git registered the worktree, then the command was killed mid-checkout.
        result = real_run(argv, cwd, timeout_seconds, lock_file)
        return {**result, "exit_code": None, "timed_out": True}

    monkeypatch.setattr(ops, "_run_bounded_command", _register_then_time_out)
    failed = ops.git_pull()
    assert failed["exit_code"] is None
    registered = subprocess.run(["git", "-C", str(source), "worktree", "list", "--porcelain"],
                                capture_output=True, text=True, check=True).stdout
    assert str(releases / revision) not in registered

    monkeypatch.setattr(ops, "_run_bounded_command", real_run)
    retried = ops.git_pull()

    assert retried["exit_code"] == 0
    assert retried["release"] == revision


def test_reconcile_boot_runs_the_rebuilt_source_head_over_a_stale_pending_release(monkeypatch, tmp_path):
    _, source, releases = _staged_repo(tmp_path)
    r1 = _commit(source, "r1")
    _boot_env(monkeypatch, tmp_path, source, releases, build_id="build-1")
    assert os.path.basename(ops.reconcile_boot_release()) == r1
    # R1 was staged as rebuild-required; the operator then pulls R2 and rebuilds.
    (releases / ".pending-release").write_text(str(releases / r1) + "\n")
    r2 = _commit(source, "r2")
    _boot_env(monkeypatch, tmp_path, source, releases, build_id="build-2")

    active = ops.reconcile_boot_release()

    assert os.path.basename(active) == r2
    assert (releases / "current").resolve() == (releases / r2).resolve()
    assert not (releases / ".pending-release").exists()
    assert (releases / ".image-build-id").read_text().strip() == "build-2"


def test_reconcile_boot_keeps_the_running_release_when_the_image_is_unchanged(monkeypatch, tmp_path):
    _, source, releases = _staged_repo(tmp_path)
    _commit(source, "r1")
    _boot_env(monkeypatch, tmp_path, source, releases, build_id="build-1")
    ops.reconcile_boot_release()
    # A code-only in-panel update moved `current` ahead of the source checkout.
    r2 = _commit(source, "r2")
    subprocess.run(["git", "-C", str(source), "reset", "-q", "--hard", "HEAD~1"], check=True)
    subprocess.run(["git", "-C", str(source), "worktree", "add", "--detach", str(releases / r2), r2],
                   check=True, capture_output=True)
    ops._switch_active_release(str(releases / r2))

    active = ops.reconcile_boot_release()

    # A plain container restart must not roll the service back to the source HEAD.
    assert os.path.basename(active) == r2


def test_reconcile_boot_prunes_superseded_releases_but_keeps_a_rollback(monkeypatch, tmp_path):
    _, source, releases = _staged_repo(tmp_path)
    revisions = [_commit(source, f"r{i}") for i in range(4)]
    _boot_env(monkeypatch, tmp_path, source, releases, build_id="build-1")
    for index, revision in enumerate(revisions[:-1]):
        subprocess.run(["git", "-C", str(source), "worktree", "add", "--detach",
                        str(releases / revision), revision], check=True, capture_output=True)
        os.utime(releases / revision, (index, index))

    active = ops.reconcile_boot_release()

    remaining = sorted(name for name in os.listdir(releases) if ops._is_revision(name))
    assert os.path.basename(active) == revisions[-1]
    # Active plus the newest superseded release survive; the two oldest are gone.
    assert remaining == sorted([revisions[-1], revisions[-2]])
    registered = subprocess.run(["git", "-C", str(source), "worktree", "list", "--porcelain"],
                                capture_output=True, text=True, check=True).stdout
    assert revisions[0] not in registered


def test_git_pull_capability_requires_a_persistent_config_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_DIR", str(tmp_path))
    monkeypatch.setattr(ops, "RELEASES_DIR", str(tmp_path / "releases"))
    monkeypatch.setattr(ops, "CONFIG_DIR", "")
    monkeypatch.setattr(
        ops.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="true\n"))

    result = ops.git_pull_capability()

    assert result["available"] is False
    assert "ACHORD_REVIEW_CONFIG_DIR" in result["reason"]


def test_rebuild_required_flags_removal_of_all_dependency_files(monkeypatch, tmp_path):
    baked = tmp_path / "baked"
    checkout = tmp_path / "checkout"
    (baked / "docker").mkdir(parents=True)
    (baked / "requirements.txt").write_text("requests==1\n")
    (baked / "pyproject.toml").write_text("[project]\n")
    (baked / "docker" / "Dockerfile").write_text("FROM python:3.12\n")
    checkout.mkdir()  # exists, but every tracked dependency file is gone
    monkeypatch.setattr(ops, "REPO_DIR", str(checkout))
    monkeypatch.setattr(ops, "DEPS_BAKED_DIR", str(baked))

    # A present-but-empty checkout is a definite change, not inconclusive.
    assert ops.rebuild_required() is True


def test_rebuild_required_detects_a_dockerfile_change(monkeypatch, tmp_path):
    baked = tmp_path / "baked"
    checkout = tmp_path / "checkout"
    for base, dockerfile in ((baked, "FROM python:3.12\n"), (checkout, "FROM python:3.13\n")):
        (base / "docker").mkdir(parents=True)
        (base / "requirements.txt").write_text("requests==1\n")
        (base / "pyproject.toml").write_text("[project]\n")
        (base / "docker" / "Dockerfile").write_text(dockerfile)
    monkeypatch.setattr(ops, "REPO_DIR", str(checkout))
    monkeypatch.setattr(ops, "DEPS_BAKED_DIR", str(baked))

    assert ops.rebuild_required() is True


def test_check_update_does_not_claim_latest_when_upstream_resolution_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_DIR", str(tmp_path))
    monkeypatch.setattr(
        ops, "git_pull_capability", lambda: {"available": True, "reason": "ready"})
    # Fetch succeeds (e.g. with prune) but the upstream ref is gone afterwards.
    monkeypatch.setattr(ops, "_git_text", _fake_git_text({
        ("rev-parse", "--short", "HEAD"): (0, "aaaaaaa"),
        ("log", "-1", "--format=%s"): (0, "head"),
        ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"): (0, "origin/main"),
        ("fetch", "--quiet", "origin"): (0, ""),
        ("rev-parse", "--short", "origin/main"): (128, "fatal: no upstream"),
    }))

    result = ops.check_update()

    assert result["checked"] is False
    assert result["update_available"] is False
    assert "远端" in result["reason"]


def test_check_update_reports_divergence_instead_of_a_doomed_update(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_DIR", str(tmp_path))
    monkeypatch.setattr(
        ops, "git_pull_capability", lambda: {"available": True, "reason": "ready"})
    # Upstream advanced (behind=2) while the local branch has its own commit
    # (ahead=1): --ff-only would fail, so no update must be offered.
    monkeypatch.setattr(ops, "_git_text", _fake_git_text({
        ("rev-parse", "--short", "HEAD"): (0, "aaaaaaa"),
        ("log", "-1", "--format=%s"): (0, "local"),
        ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"): (0, "origin/main"),
        ("fetch", "--quiet", "origin"): (0, ""),
        ("rev-parse", "--short", "origin/main"): (0, "bbbbbbb"),
        ("log", "-1", "--format=%s", "origin/main"): (0, "remote"),
        ("rev-list", "--count", "HEAD..origin/main"): (0, "2"),
        ("rev-list", "--count", "origin/main..HEAD"): (0, "1"),
    }))

    result = ops.check_update()

    assert result["checked"] is True
    assert result["diverged"] is True
    assert result["update_available"] is False
    assert result["ahead"] == 1
    assert result["behind"] == 2


def test_git_pull_capability_explains_the_retired_in_place_mode(monkeypatch, tmp_path):
    # An upgraded deployment that still only sets REPO_DIR must learn what to add.
    monkeypatch.setattr(ops, "REPO_DIR", str(tmp_path))
    monkeypatch.setattr(ops, "RELEASES_DIR", "")

    result = ops.git_pull_capability()

    assert result["available"] is False
    assert "Migrating from in-place updates" in result["reason"]
    assert "ACHORD_REVIEW_RELEASES_DIR" in result["reason"]


def test_default_update_ref_resolves_through_the_source_checkout(monkeypatch, tmp_path):
    remote, source, releases = _staged_repo(tmp_path)
    r1 = _commit(source, "r1")
    subprocess.run(["git", "-C", str(source), "branch", "-M", "main"], check=True)
    subprocess.run(["git", "-C", str(source), "push", "-qu", "origin", "main"], check=True, capture_output=True)
    _boot_env(monkeypatch, tmp_path, source, releases, build_id="build-1")
    monkeypatch.setattr(ops, "UPDATE_REF", "@{u}")
    # `current` is a detached worktree, where @{u} can never resolve on its own.
    assert os.path.basename(ops.reconcile_boot_release()) == r1
    assert subprocess.run(["git", "-C", str(releases / "current"), "rev-parse", "--abbrev-ref", "@{u}"],
                          capture_output=True).returncode != 0

    assert ops.git_pull_capability()["available"] is True
    info = ops.check_update()

    assert info["checked"] is True
    assert info["latest"]["branch"] == "origin/main"
    assert info["update_available"] is False


def test_default_update_ref_without_an_upstream_disables_the_capability(monkeypatch, tmp_path):
    _, source, releases = _staged_repo(tmp_path)
    _commit(source, "r1")  # a clone of an empty bare remote: no upstream configured
    _boot_env(monkeypatch, tmp_path, source, releases, build_id="build-1")
    monkeypatch.setattr(ops, "UPDATE_REF", "@{u}")
    ops.reconcile_boot_release()

    result = ops.git_pull_capability()

    assert result["available"] is False
    assert "ACHORD_REVIEW_UPDATE_REF" in result["reason"]


def test_activation_rolls_the_link_back_when_the_marker_cannot_be_removed(monkeypatch, tmp_path):
    releases, old_release, new_release, active = _pending_release_layout(tmp_path, monkeypatch)
    real_unlink = ops.os.unlink

    def _refuse_marker(path, *args, **kwargs):
        if path == ops._pending_marker_path():
            raise PermissionError("read-only releases volume")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(ops.os, "unlink", _refuse_marker)

    with pytest.raises(PermissionError):
        ops._activate_pending_release()

    # `current` never outruns the process serving it; the marker is still there.
    assert active.resolve() == old_release.resolve()
    assert (releases / ".pending-release").read_text().strip() == str(new_release)


def test_reconcile_boot_fails_closed_when_first_boot_head_deps_differ(monkeypatch, tmp_path):
    # Image built from deps "a==1", but the mounted checkout's HEAD needs "b==2":
    # booting it would run new code against the image's old packages, so on a first
    # boot (nothing already serving) the launcher must refuse rather than loop.
    _, source, releases = _staged_repo(tmp_path)
    _commit_dep(source, "requirements.txt", "b==2\n")
    baked = tmp_path / "baked"
    baked.mkdir()
    (baked / "build-id").write_text("build-1\n")
    (baked / "requirements.txt").write_text("a==1\n")
    monkeypatch.setattr(ops, "DEPS_BAKED_DIR", str(baked))
    monkeypatch.setattr(ops, "SOURCE_DIR", str(source))
    monkeypatch.setattr(ops, "RELEASES_DIR", str(releases))
    monkeypatch.setattr(ops, "REPO_DIR", str(releases / "current"))

    with pytest.raises(OSError):
        ops.reconcile_boot_release()

    # The build stamp is not consumed, so a corrected rebuild is still detected.
    assert not (releases / ".image-build-id").exists()
    assert not (releases / "current").exists()


def test_reconcile_boot_keeps_current_when_a_rebuilt_head_is_dep_incompatible(monkeypatch, tmp_path):
    _, source, releases = _staged_repo(tmp_path)
    a = _commit_dep(source, "requirements.txt", "a==1\n")
    baked = tmp_path / "baked"
    baked.mkdir()
    (baked / "build-id").write_text("build-1\n")
    (baked / "requirements.txt").write_text("a==1\n")
    monkeypatch.setattr(ops, "DEPS_BAKED_DIR", str(baked))
    monkeypatch.setattr(ops, "SOURCE_DIR", str(source))
    monkeypatch.setattr(ops, "RELEASES_DIR", str(releases))
    monkeypatch.setattr(ops, "REPO_DIR", str(releases / "current"))
    assert os.path.basename(ops.reconcile_boot_release()) == a

    # The checkout advances to a dependency-changing commit and the stamp changes,
    # but the image's baked deps still match A, not B.
    b = _commit_dep(source, "requirements.txt", "b==2\n")
    (baked / "build-id").write_text("build-2\n")

    active = ops.reconcile_boot_release()

    # B is refused; the compatible release A keeps serving; B waits as pending; and
    # the build-2 stamp is left unconsumed so the corrected rebuild still activates.
    assert os.path.basename(active) == a
    assert (releases / "current").resolve() == (releases / a).resolve()
    assert (releases / ".image-build-id").read_text().strip() == "build-1"
    assert ops._pending_release() == str((releases / b).resolve())


def test_rebuild_required_flags_a_boot_reconciler_change(monkeypatch, tmp_path):
    # The launcher runs the image's baked reconciler, so a staged change to it must
    # count as rebuild-required rather than a restart-safe code-only update.
    baked = tmp_path / "baked"
    checkout = tmp_path / "checkout"
    for base, body in ((baked, "reconcile = 'old'\n"), (checkout, "reconcile = 'new'\n")):
        (base / "pr_agent" / "dashboard").mkdir(parents=True)
        (base / "pr_agent" / "dashboard" / "ops.py").write_text(body)
    monkeypatch.setattr(ops, "REPO_DIR", str(checkout))
    monkeypatch.setattr(ops, "DEPS_BAKED_DIR", str(baked))

    assert ops.rebuild_required() is True


def test_ensure_release_worktree_recreates_an_incomplete_checkout(monkeypatch, tmp_path):
    _, source, releases = _staged_repo(tmp_path)
    revision = _commit(source, "r1")
    monkeypatch.setattr(ops, "SOURCE_DIR", str(source))
    monkeypatch.setattr(ops, "RELEASES_DIR", str(releases))
    monkeypatch.setattr(ops, "REPO_DIR", str(releases / "current"))

    path = ops._ensure_release_worktree(revision)
    assert ops._release_is_complete(path)

    # Simulate an interruption after registration but before the checkout finished.
    os.unlink(ops._release_complete_marker(path))
    os.unlink(os.path.join(path, "value.txt"))

    rebuilt = ops._ensure_release_worktree(revision)

    assert rebuilt == path
    assert ops._release_is_complete(rebuilt)
    with open(os.path.join(rebuilt, "value.txt")) as handle:
        assert handle.read() == "r1\n"


def test_discard_release_worktree_prunes_registration_immediately(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "")

    monkeypatch.setattr(ops.subprocess, "run", fake_run)
    monkeypatch.setattr(ops.shutil, "rmtree", lambda *a, **k: None)
    monkeypatch.setattr(ops, "SOURCE_DIR", str(tmp_path / "src"))

    ops._discard_release_worktree(str(tmp_path / "src" / "releases" / ("d" * 40)))

    prune = next(c for c in calls if "prune" in c)
    assert prune[-2:] == ["--expire", "now"]


def test_git_pull_rebuilds_a_release_missing_its_completion_marker(monkeypatch, tmp_path):
    remote, source, releases = _staged_repo(tmp_path)
    _commit(source, "old")
    subprocess.run(["git", "-C", str(source), "branch", "-M", "main"], check=True)
    subprocess.run(["git", "-C", str(source), "push", "-qu", "origin", "main"], check=True, capture_output=True)
    writer = tmp_path / "writer"
    subprocess.run(["git", "clone", str(remote), str(writer)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(writer), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(writer), "config", "user.name", "T"], check=True)
    (writer / "value.txt").write_text("new\n")
    subprocess.run(["git", "-C", str(writer), "commit", "-qam", "new"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(writer), "push", "-q"], check=True, capture_output=True)
    (releases / "current").symlink_to(source)
    monkeypatch.setattr(ops, "REPO_DIR", str(releases / "current"))
    monkeypatch.setattr(ops, "RELEASES_DIR", str(releases))
    monkeypatch.setattr(ops, "UPDATE_REF", "origin/main")
    monkeypatch.setattr(ops, "DEPS_BAKED_DIR", str(source))

    first = ops.git_pull()
    release_path = str(releases / first["release"])
    assert ops._release_is_complete(release_path)

    # An interrupted retry left the directory registered but incomplete.
    os.unlink(ops._release_complete_marker(release_path))
    os.unlink(os.path.join(release_path, "value.txt"))

    second = ops.git_pull()

    assert second["release"] == first["release"]
    assert ops._release_is_complete(release_path)
    with open(os.path.join(release_path, "value.txt")) as handle:
        assert handle.read() == "new\n"


def test_check_update_fetches_the_comparison_refs_own_remote(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_DIR", str(tmp_path))
    monkeypatch.setattr(
        ops, "git_pull_capability", lambda: {"available": True, "reason": "ready"})
    monkeypatch.setattr(ops, "UPDATE_REF", "upstream/main")
    fetched = []

    def _git(args, timeout):
        if args[:1] == ["fetch"]:
            fetched.append(args)
            return (0, "")
        return {
            ("rev-parse", "--short", "HEAD"): (0, "aaaaaaa"),
            ("log", "-1", "--format=%s"): (0, "head"),
            ("rev-parse", "--short", "upstream/main"): (0, "bbbbbbb"),
            ("log", "-1", "--format=%s", "upstream/main"): (0, "remote"),
            ("rev-list", "--count", "HEAD..upstream/main"): (0, "1"),
            ("rev-list", "--count", "upstream/main..HEAD"): (0, "0"),
        }[tuple(args)]

    monkeypatch.setattr(ops, "_git_text", _git)

    result = ops.check_update()

    # The multi-remote checkout must fetch `upstream`, not Git's default `origin`.
    assert fetched == [["fetch", "--quiet", "upstream"]]
    assert result["update_available"] is True


def test_extract_container_ids_prefers_the_container_path_over_overlay_layers():
    layer = "a" * 64
    container = "b" * 64
    blob = (
        f"41 30 0:35 / /var/lib/docker/overlay2/{layer}/merged rw shared\n"
        f"52 41 0:36 /{container}/hostname /etc/hostname rw,relatime\n"
        f"53 41 0:36 / /var/lib/docker/containers/{container}/mounts rw\n")

    assert ops._extract_container_ids(blob) == {container}


def test_extract_container_ids_falls_back_to_any_hash_without_a_container_path():
    only = "c" * 64
    assert ops._extract_container_ids(f"anon path with {only} inside") == {only}


def test_docker_target_is_self_matches_the_real_id_past_overlay_noise(monkeypatch):
    container = "b" * 64
    monkeypatch.setattr(
        ops.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=container + "\n"))
    # A layer hash sorts first, but the real container id is also a candidate.
    monkeypatch.setattr(ops, "_own_container_ids", lambda: {"a" * 64, container})

    assert ops._docker_target_is_self() is True


def test_switch_active_release_recovers_from_a_stale_temporary_link(monkeypatch, tmp_path):
    releases = tmp_path / "releases"
    old_release = releases / "old"
    new_release = releases / "new"
    old_release.mkdir(parents=True)
    new_release.mkdir()
    active = releases / "current"
    active.symlink_to(old_release)
    monkeypatch.setattr(ops, "REPO_DIR", str(active))
    # A crash on a previous boot with this PID left a temporary link behind.
    stale = f"{active}.{os.getpid()}.tmp"
    os.symlink(old_release, stale)

    previous = ops._switch_active_release(str(new_release))

    assert active.resolve() == new_release.resolve()
    assert os.path.realpath(previous) == str(old_release)
    assert not os.path.lexists(stale)
