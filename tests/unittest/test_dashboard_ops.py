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
        ops, "git_pull_capability", lambda: {"available": True, "reason": "ready"})
    monkeypatch.setattr(ops, "rebuild_required", lambda: False)
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
        "dependencies_changed": False,
    }


def test_git_pull_reports_not_started_without_binary(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(ops, "_run_bounded_command", missing)
    monkeypatch.setattr(
        ops, "git_pull_capability", lambda: {"available": True, "reason": "ready"})

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
            args=args[0], returncode=0, stdout="container found"))
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
        ("fetch", "--quiet"): (0, ""),
        ("rev-parse", "--short", "@{u}"): (0, "bbbbbbb"),
        ("log", "-1", "--format=%s", "@{u}"): (0, "new commit"),
        ("rev-list", "--count", "HEAD..@{u}"): (0, "3"),
        ("rev-list", "--count", "@{u}..HEAD"): (0, "0"),
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
        ("fetch", "--quiet"): (0, ""),
        ("rev-parse", "--short", "@{u}"): (0, "aaaaaaa"),
        ("log", "-1", "--format=%s", "@{u}"): (0, "head"),
        ("rev-list", "--count", "HEAD..@{u}"): (0, "0"),
        ("rev-list", "--count", "@{u}..HEAD"): (0, "0"),
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
        ("fetch", "--quiet"): (1, "fatal: could not read from remote"),
    }))

    result = ops.check_update()

    assert result["checked"] is False
    assert result["update_available"] is False
    assert "could not read from remote" in result["reason"]


def test_restart_capability_prefers_docker_when_endpoint_is_live(monkeypatch):
    monkeypatch.setattr(
        ops.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout="[]"))
    monkeypatch.setattr(ops, "SELF_RESTART_ENABLED", True)

    capability = ops.restart_capability()

    assert capability["available"] is True
    assert capability["mode"] == "docker"


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
    monkeypatch.setattr(
        ops, "git_pull_capability", lambda: {"available": True, "reason": "ready"})
    monkeypatch.setattr(ops, "_run_bounded_command", _completed_pull)
    monkeypatch.setattr(ops, "rebuild_required", lambda: True)

    result = ops.git_pull()

    assert result["dependencies_changed"] is True
    assert any("--build" in line for line in result["output"])


def test_git_pull_survives_a_dependency_check_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_DIR", str(tmp_path))
    monkeypatch.setattr(
        ops, "git_pull_capability", lambda: {"available": True, "reason": "ready"})
    monkeypatch.setattr(ops, "_run_bounded_command", _completed_pull)

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
        ("fetch", "--quiet"): (0, ""),
        ("rev-parse", "--short", "@{u}"): (128, "fatal: no upstream"),
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
        ("fetch", "--quiet"): (0, ""),
        ("rev-parse", "--short", "@{u}"): (0, "bbbbbbb"),
        ("log", "-1", "--format=%s", "@{u}"): (0, "remote"),
        ("rev-list", "--count", "HEAD..@{u}"): (0, "2"),
        ("rev-list", "--count", "@{u}..HEAD"): (0, "1"),
    }))

    result = ops.check_update()

    assert result["checked"] is True
    assert result["diverged"] is True
    assert result["update_available"] is False
    assert result["ahead"] == 1
    assert result["behind"] == 2
