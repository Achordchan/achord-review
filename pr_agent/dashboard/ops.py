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
RESTART_ACCEPTANCE_GRACE_SECONDS = 0.2
RESTART_COMMAND_TIMEOUT_SECONDS = 45
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


def _retain_operation_lock(lock_file, proc: subprocess.Popen) -> bool:
    """Keep a duplicated flock alive until a surviving child actually exits."""
    if lock_file is None:
        return False
    try:
        retained_fd = os.dup(lock_file.fileno())
    except OSError:
        return False

    def _wait_and_release() -> None:
        try:
            proc.wait()
        except Exception as e:
            get_logger().warning(f"Dashboard command exit monitor failed, error: {e}")
        finally:
            os.close(retained_fd)

    threading.Thread(
        target=_wait_and_release, name="dashboard-command-lock-retainer", daemon=True).start()
    return True


def _run_bounded_command(argv: List[str], cwd: str, timeout_seconds: int,
                         lock_file=None) -> Dict[str, Any]:
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
    kill_wait_expired = False
    lock_retained = False
    try:
        exit_code = proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            exit_code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            kill_wait_expired = True
            exit_code = None
            lock_retained = _retain_operation_lock(lock_file, proc)
    reader.join(timeout=5)
    output = output_tail.decode("utf-8", errors="replace").splitlines()
    if timed_out:
        output.append(f"命令超过 {timeout_seconds} 秒，已终止")
    if kill_wait_expired:
        lock_state = "运维锁将保持到进程退出" if lock_retained else "无法确认运维锁保持状态"
        output.append(f"进程组收到强制终止信号后仍未退出，{lock_state}")
    return {"started": True, "completed": not kill_wait_expired,
            "exit_code": None if timed_out else exit_code,
            "timed_out": timed_out, "lock_retained": lock_retained,
            "output": output}


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
            restart_process = subprocess.Popen(
                ["docker", "restart", "--timeout", "30", CONTAINER_NAME],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                pass_fds=(lock_file.fileno(),))
            time.sleep(RESTART_ACCEPTANCE_GRACE_SECONDS)
            return_code = restart_process.poll()
            if return_code not in (None, 0):
                return _not_started(f"docker restart 立即失败（退出码 {return_code}）")
            if return_code == 0:
                return {"started": True, "completed": True, "exit_code": 0, "output": []}
            _monitor_restart_process(restart_process)
            return {"started": True, "completed": False, "exit_code": None, "output": []}
        except subprocess.TimeoutExpired:
            return _not_started("Docker 端点预检超时，容器重启未发起")
        except OSError:
            return _not_started("docker CLI 或受控 Docker 端点不可用，容器重启未发起")


def _monitor_restart_process(proc: subprocess.Popen) -> threading.Thread:
    """Terminate a restart CLI that exceeds Docker's own restart deadline."""
    def _monitor() -> None:
        try:
            proc.wait(timeout=RESTART_COMMAND_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                get_logger().warning("docker restart process survived the monitor SIGKILL")

    monitor = threading.Thread(
        target=_monitor, name="dashboard-restart-monitor", daemon=True)
    monitor.start()
    return monitor


def git_pull() -> Dict[str, Any]:
    with _operation_lock() as lock_file:
        if lock_file is None:
            return _not_started("另一项运维操作正在执行，git pull 未发起")
        try:
            return _run_bounded_command(
                ["git", "-C", REPO_DIR, "pull", "--ff-only"],
                cwd=REPO_DIR, timeout_seconds=GIT_PULL_TIMEOUT_SECONDS,
                lock_file=lock_file)
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
    """Validate the App JWT, an active installation, and repository access."""
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
        app_token = jwt.encode(payload, private_key, algorithm="RS256")
        import requests
        app_headers = {"Authorization": f"Bearer {app_token}",
                       "Accept": "application/vnd.github+json"}
        app_response = requests.get(
            "https://api.github.com/app", timeout=15, headers=app_headers)
        if app_response.status_code != 200:
            return {"ok": False, "app_id": app_id,
                    "error": f"GitHub App API returned {app_response.status_code}"}
        installation = None
        installations_url = "https://api.github.com/app/installations?per_page=100"
        while installations_url:
            installations_response = requests.get(
                installations_url, timeout=15, headers=app_headers)
            if installations_response.status_code != 200:
                return {"ok": False, "app_id": app_id,
                        "error": f"GitHub installations API returned {installations_response.status_code}"}
            installations = installations_response.json()
            installation = next(
                (item for item in installations if not item.get("suspended_at")), None)
            if installation is not None:
                break
            installations_url = _next_link(
                getattr(installations_response, "headers", {}).get("Link", ""))
        if installation is None:
            return {"ok": False, "app_id": app_id, "error": "no active GitHub App installation"}

        installation_id = int(installation["id"])
        token_response = requests.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            timeout=15, headers=app_headers)
        if token_response.status_code != 201:
            return {"ok": False, "app_id": app_id, "installation_id": installation_id,
                    "error": f"GitHub installation token API returned {token_response.status_code}"}
        installation_token = str(token_response.json().get("token", ""))
        if not installation_token:
            return {"ok": False, "app_id": app_id, "installation_id": installation_id,
                    "error": "GitHub installation token response was empty"}
        installation_headers = {
            "Authorization": f"Bearer {installation_token}",
            "Accept": "application/vnd.github+json",
        }
        try:
            repositories_response = requests.get(
                "https://api.github.com/installation/repositories?per_page=1",
                timeout=15, headers=installation_headers)
            if repositories_response.status_code != 200:
                return {"ok": False, "app_id": app_id, "installation_id": installation_id,
                        "error": f"GitHub repository access returned {repositories_response.status_code}"}
            return {
                "ok": True,
                "app_id": app_id,
                "app_name": app_response.json().get("name", ""),
                "installation_id": installation_id,
                "repository_count": int(repositories_response.json().get("total_count", 0)),
            }
        finally:
            try:
                requests.delete(
                    "https://api.github.com/installation/token",
                    timeout=15, headers=installation_headers)
            except Exception as e:
                get_logger().debug(f"GitHub probe token revocation skipped, error: {e}")
    except Exception as e:
        return {"ok": False, "app_id": app_id, "error": str(e)[:300]}


def _next_link(link_header: str) -> str:
    for part in (link_header or "").split(","):
        target, separator, relation = part.strip().partition(";")
        if separator and 'rel="next"' in relation and target.startswith("<") and target.endswith(">"):
            return target[1:-1]
    return ""


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
