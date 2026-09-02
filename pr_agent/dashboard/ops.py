"""One-click operations and self-diagnosis for the dashboard.

Ops commands are restricted to a fixed whitelist - there is no generic shell
endpoint. Restart and git-pull shell out to the container runtime / git binary
and stream their output back to the caller; probes (LLM relay, GitHub App
credential, storage) run in-process and never raise.
"""

import os
import subprocess
import time
from typing import Any, Dict, List

from pr_agent.log import get_logger

# Fixed commands only; nothing constructed from user input reaches the shell.
CONTAINER_NAME = os.environ.get("ACHORD_REVIEW_CONTAINER", "achord-review")
REPO_DIR = os.environ.get("ACHORD_REVIEW_REPO_DIR", "/app")
_RUNNING_PROC: Dict[str, subprocess.Popen] = {}
_RUNNING_LOGS: Dict[str, List[str]] = {}


def _run_tracked(key: str, argv: List[str], cwd: str = "/") -> Dict[str, Any]:
    """Start a whitelisted command, keep its handle, return a task id."""
    if _RUNNING_PROC.get(key) and _RUNNING_PROC[key].poll() is None:
        return {"task_id": key, "already_running": True, "output": _RUNNING_LOGS.get(key, [])}
    _RUNNING_LOGS[key] = []
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    _RUNNING_PROC[key] = proc
    return {"task_id": key, "already_running": False}


def _poll(key: str) -> Dict[str, Any]:
    proc = _RUNNING_PROC.get(key)
    if proc is None:
        return {"running": False, "exists": False, "output": _RUNNING_LOGS.get(key, [])}
    # drain whatever the process has produced so far without blocking: the
    # output pipe is nonblocking, so a silent or stalled command cannot hang
    # the API request (and with it the shared webhook event loop)
    logs = _RUNNING_LOGS.setdefault(key, [])
    if proc.stdout:
        os.set_blocking(proc.stdout.fileno(), False)
        try:
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                logs.append(line.rstrip())
        except (BlockingIOError, OSError):
            pass  # nothing buffered right now; try again on the next poll
    running = proc.poll() is None
    return {"running": running, "exists": True, "exit_code": None if running else proc.returncode,
            "output": logs}


def restart_container() -> Dict[str, Any]:
    """Restart the achord-review container via the docker socket mount.

    Requires the host docker CLI + socket to be available inside this
    container (mount /var/run/docker.sock read-only to enable); without it
    the task output explains the missing prerequisite instead of the API
    silently claiming a restart was issued.
    """
    argv = ["docker", "restart", "--timeout", "30", CONTAINER_NAME]
    try:
        return _run_tracked("restart", argv)
    except FileNotFoundError:
        return {"task_id": "restart", "already_running": False,
                "output": ["docker CLI not available inside this container - "
                           "mount /var/run/docker.sock (read-only) to enable one-click restart"]}


def git_pull() -> Dict[str, Any]:
    argv = ["git", "-C", REPO_DIR, "pull", "--ff-only"]
    try:
        return _run_tracked("git-pull", argv, cwd=REPO_DIR)
    except FileNotFoundError:
        return {"task_id": "git-pull", "already_running": False,
                "output": ["git not available inside this container"]}


def poll_task(task_id: str) -> Dict[str, Any]:
    if task_id not in ("restart", "git-pull"):
        return {"running": False, "exists": False, "output": []}
    return _poll(task_id)


# ------------------------------------------------------------------ probes

def probe_llm(timeout_seconds: int = 30) -> Dict[str, Any]:
    """Send a tiny chat request through the configured relay and time it."""
    from pr_agent.config_loader import get_settings
    settings = get_settings()
    base_url = str(settings.get("openai.api_base", "")).strip()
    key = str(settings.get("openai.key", "")).strip()
    model = str(settings.get("config.model", "")).strip()
    if not base_url or not key:
        return {"ok": False, "error": "openai.api_base / openai.key are not configured"}
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key, base_url=base_url, timeout=timeout_seconds, max_retries=0)
        start = time.monotonic()
        client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5,
        )
        latency_ms = int((time.monotonic() - start) * 1000)
        return {"ok": True, "model": model, "base_url": base_url, "latency_ms": latency_ms}
    except Exception as e:
        return {"ok": False, "model": model, "base_url": base_url, "error": str(e)[:300]}


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
            while pos > 0 and data.count(b"\n") <= max_lines:
                read = min(chunk_size, pos)
                pos -= read
                f.seek(pos)
                data = f.read(read) + data
        lines = data.decode(errors="replace").splitlines()
        return lines[-max_lines:]
    except Exception as e:
        get_logger().warning(f"Dashboard log tail failed, error: {e}")
    return []
