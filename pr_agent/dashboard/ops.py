"""One-click operations and self-diagnosis for the dashboard.

Ops commands are restricted to a fixed whitelist - there is no generic shell
endpoint. Restart and git-pull shell out to the container runtime / git binary
from an API worker thread. Git pull returns its bounded final output directly;
restart returns once the controlled runtime accepts the command. Probes (LLM
relay, GitHub App credential, storage) run in-process and never raise.
"""

import asyncio
import fcntl
import os
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, List

from pr_agent.log import get_logger

# Fixed commands only; nothing constructed from user input reaches the shell.
CONTAINER_NAME = os.environ.get("ACHORD_REVIEW_CONTAINER", "achord-review")
REPO_DIR = os.environ.get("ACHORD_REVIEW_REPO_DIR", "/app")
GIT_PULL_TIMEOUT_SECONDS = 120
DOCKER_PREFLIGHT_TIMEOUT_SECONDS = 5
MAX_LOG_TAIL_BYTES = 2 * 1024 * 1024
MAX_GIT_OUTPUT_BYTES = 1024 * 1024
OPS_LOCK_PATH = os.environ.get("DASHBOARD_OPS_LOCK_PATH", "/app/data/dashboard-ops.lock")


def _not_started(message: str) -> Dict[str, Any]:
    return {"started": False, "completed": True, "exit_code": None, "output": [message]}


@contextmanager
def _operation_lock():
    """Acquire the cross-worker operations lock without waiting."""
    try:
        directory = os.path.dirname(OPS_LOCK_PATH) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd = os.open(OPS_LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
        lock_file = os.fdopen(fd, "a+")
    except OSError:
        yield None
        return
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        yield lock_file
    finally:
        # Do not explicitly unlock: restart passes this fd to the Docker child,
        # which keeps the lock until the restart command exits or kills us.
        lock_file.close()


def _run_bounded_command(argv: List[str], cwd: str, timeout_seconds: int) -> Dict[str, Any]:
    """Run a fixed command while retaining only its last bounded output bytes."""
    proc = subprocess.Popen(
        argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True)
    output_tail = bytearray()

    def _drain() -> None:
        if proc.stdout is None:
            return
        while chunk := proc.stdout.read(64 * 1024):
            output_tail.extend(chunk)
            if len(output_tail) > MAX_GIT_OUTPUT_BYTES:
                del output_tail[:-MAX_GIT_OUTPUT_BYTES]

    reader = threading.Thread(target=_drain, name="dashboard-git-output", daemon=True)
    reader.start()
    timed_out = False
    try:
        exit_code = proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        exit_code = proc.wait(timeout=5)
    reader.join(timeout=5)
    output = output_tail.decode("utf-8", errors="replace").splitlines()
    if timed_out:
        output.append(f"命令超过 {timeout_seconds} 秒，已终止")
    return {"started": True, "completed": True,
            "exit_code": None if timed_out else exit_code,
            "timed_out": timed_out, "output": output}


def restart_container() -> Dict[str, Any]:
    """Restart the achord-review container through a configured Docker endpoint.

    The default deployment intentionally exposes no raw Docker socket. An
    operator may supply a narrowly authorized Docker endpoint; otherwise the
    explicit not-started result lets the API and UI report the unavailable
    operation without creating process-local task state.
    """
    with _operation_lock() as lock_file:
        if lock_file is None:
            return _not_started("另一项运维操作正在执行，容器重启未发起")
        try:
            preflight = subprocess.run(
                ["docker", "inspect", CONTAINER_NAME], stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, timeout=DOCKER_PREFLIGHT_TIMEOUT_SECONDS,
                check=False)
            if preflight.returncode != 0:
                return _not_started((preflight.stdout or "Docker 端点不可用").strip()[:1000])
            subprocess.Popen(
                ["docker", "restart", "--timeout", "30", CONTAINER_NAME],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                pass_fds=(lock_file.fileno(),))
            return {"started": True, "completed": False, "exit_code": None, "output": []}
        except subprocess.TimeoutExpired:
            return _not_started("Docker 端点预检超时，容器重启未发起")
        except OSError:
            return _not_started("docker CLI 或受控 Docker 端点不可用，容器重启未发起")


def git_pull() -> Dict[str, Any]:
    with _operation_lock() as lock_file:
        if lock_file is None:
            return _not_started("另一项运维操作正在执行，git pull 未发起")
        try:
            return _run_bounded_command(
                ["git", "-C", REPO_DIR, "pull", "--ff-only"],
                cwd=REPO_DIR, timeout_seconds=GIT_PULL_TIMEOUT_SECONDS)
        except OSError:
            return _not_started("git 或代码目录不可用，git pull 未发起")


# ------------------------------------------------------------------ probes

def probe_llm(timeout_seconds: int = 30) -> Dict[str, Any]:
    """Send a tiny chat request through the same LiteLLM adapter as reviews."""
    from pr_agent.config_loader import get_settings
    settings = get_settings()
    base_url = str(settings.get("openai.api_base", "")).strip()
    model = str(settings.get("config.model", "")).strip()
    try:
        handler = _get_probe_ai_handler()
        start = time.monotonic()
        response, _ = asyncio.run(asyncio.wait_for(
            handler.chat_completion(
                model=model, system="Return a minimal health-check response.",
                user="Reply with exactly: pong", temperature=0),
            timeout=timeout_seconds))
        latency_ms = int((time.monotonic() - start) * 1000)
        return {"ok": bool(response), "model": model, "base_url": base_url,
                "latency_ms": latency_ms}
    except TimeoutError:
        return {"ok": False, "model": model, "base_url": base_url,
                "error": f"LLM probe timed out after {timeout_seconds} seconds"}
    except Exception as e:
        return {"ok": False, "model": model, "base_url": base_url, "error": str(e)[:300]}


def _get_probe_ai_handler():
    from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
    return LiteLLMAIHandler()


def probe_github_app() -> Dict[str, Any]:
    """Validate the GitHub App private key by minting an installation token."""
    from pr_agent.config_loader import get_settings
    settings = get_settings()
    app_id = str(settings.get("github.app_id", "")).strip()
    private_key = str(settings.get("github.private_key", "")).strip()
    if not app_id or not private_key:
        return {"ok": False, "error": "github.app_id / github.private_key are not configured"}
    try:
        import jwt
        now = int(time.time())
        payload = {"iat": now, "exp": now + 600, "iss": app_id}
        token = jwt.encode(payload, private_key, algorithm="RS256")
        import requests
        resp = requests.get("https://api.github.com/app", timeout=15,
                            headers={"Authorization": f"Bearer {token}",
                                     "Accept": "application/vnd.github+json"})
        if resp.status_code != 200:
            return {"ok": False, "app_id": app_id,
                    "error": f"GitHub API returned {resp.status_code}"}
        return {"ok": True, "app_id": app_id, "app_name": resp.json().get("name", "")}
    except Exception as e:
        return {"ok": False, "app_id": app_id, "error": str(e)[:300]}


def probe_storage() -> Dict[str, Any]:
    from pr_agent.dashboard.storage import get_storage
    return get_storage().health()


def diagnose() -> Dict[str, Any]:
    """Run every probe; each failure is captured, never raised."""
    results = {"llm": probe_llm(), "github_app": probe_github_app(), "storage": probe_storage()}
    results["ok"] = all(result.get("ok") for result in results.values())
    return results


def tail_logs(max_lines: int = 200) -> List[str]:
    """Best-effort recent log lines for the ops console.

    Seeks backwards from the end of the file instead of reading it whole: the
    ops page polls this every few seconds and a multi-hundred-MB production
    log must not be slurped into memory each time.
    """
    try:
        log_file = os.environ.get("ACHORD_REVIEW_LOG_FILE", "")
        if not log_file or not os.path.isfile(log_file):
            return []
        chunk_size = 64 * 1024
        with open(log_file, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            data = b""
            pos = size
            while (pos > 0 and data.count(b"\n") <= max_lines
                   and len(data) < MAX_LOG_TAIL_BYTES):
                read = min(chunk_size, pos, MAX_LOG_TAIL_BYTES - len(data))
                pos -= read
                f.seek(pos)
                data = f.read(read) + data
        lines = data.decode(errors="replace").splitlines()
        return lines[-max_lines:]
    except Exception as e:
        get_logger().warning(f"Dashboard log tail failed, error: {e}")
    return []
