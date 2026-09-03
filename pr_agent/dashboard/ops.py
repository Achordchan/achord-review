"""One-click operations and self-diagnosis for the dashboard.

Ops commands are restricted to a fixed whitelist - there is no generic shell
endpoint. Git pull returns its bounded final output from an API worker thread.
Self-restart is prepared under the shared lock, then executed as a FastAPI
post-response task so the acknowledgment and audit reach the client first.
Probes (LLM relay, GitHub App credential, storage) never raise.
"""

import asyncio
import fcntl
import hashlib
import os
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from pr_agent.log import get_logger

# Fixed commands only; nothing constructed from user input reaches the shell.
CONTAINER_NAME = os.environ.get("ACHORD_REVIEW_CONTAINER", "achord-review")
REPO_DIR = os.environ.get("ACHORD_REVIEW_REPO_DIR", "").strip()
# Socket-free self-restart: terminate the gunicorn master (PID 1) and let the
# container's `restart: unless-stopped` policy bring a fresh process up, which
# re-imports code from the mounted checkout. Opt-in, because it is only safe
# when the container actually carries a restart policy (the shipped compose does).
SELF_RESTART_ENABLED = os.environ.get("ACHORD_REVIEW_SELF_RESTART", "").strip().lower() in (
    "1", "true", "yes", "on")
# The dependency-defining files, and where the image keeps the copy it was built
# from (see docker/Dockerfile). A restart only re-imports Python — it cannot
# install packages — so when a mounted checkout's dependencies diverge from the
# running image's, an in-place restart would boot code against stale deps and, under
# `restart: unless-stopped`, loop. We compare fingerprints to detect and block that.
DEPS_FILES = ("requirements.txt", "pyproject.toml", "docker/Dockerfile")
DEPS_BAKED_DIR = os.environ.get("ACHORD_DEPS_BAKED_DIR", "/app/.deps-baked")
GIT_PULL_TIMEOUT_SECONDS = 120
GIT_FETCH_TIMEOUT_SECONDS = 45
GIT_PREFLIGHT_TIMEOUT_SECONDS = 5
DOCKER_PREFLIGHT_TIMEOUT_SECONDS = 5
RESTART_COMMAND_TIMEOUT_SECONDS = 45
# Revoking a probe's installation token must not depend on whatever is left of
# the probe deadline: an unrevoked token stays valid for an hour.
TOKEN_REVOCATION_TIMEOUT_SECONDS = 5
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
    exit_code = None
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
            # The process exited between wait() timing out and signal delivery.
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            kill_wait_expired = True
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


class _RestartTicket:
    """Transfer the held operations lock into an after-response restart task."""

    def __init__(self, lock_context, lock_file, mode: str = "docker"):
        self.lock_context = lock_context
        self.lock_file = lock_file
        self.mode = mode


def _self_restart_capability() -> Optional[Dict[str, Any]]:
    """Socket-free fallback: exit PID 1 and let the restart policy respawn us."""
    if not SELF_RESTART_ENABLED:
        return None
    return {"available": True, "mode": "self",
            "reason": "将通过退出进程、由容器重启策略自动拉起（无需 Docker 端点）。"}


def restart_capability() -> Dict[str, Any]:
    """Report how a restart can happen: a Docker endpoint, or a socket-free self-exit."""
    try:
        preflight = subprocess.run(
            ["docker", "inspect", CONTAINER_NAME], stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, timeout=DOCKER_PREFLIGHT_TIMEOUT_SECONDS,
            check=False)
    except subprocess.TimeoutExpired:
        preflight = None
    except OSError:
        preflight = None
    if preflight is not None and preflight.returncode == 0:
        return {"available": True, "mode": "docker",
                "reason": "已连接受控 Docker 端点，重启将在响应后执行。"}
    # Docker endpoint absent or unusable — fall back to a self-exit if enabled.
    self_restart = _self_restart_capability()
    if self_restart is not None:
        return self_restart
    if preflight is not None and preflight.returncode != 0:
        return {"available": False, "reason": (preflight.stdout or "Docker 端点不可用").strip()[:300]}
    return {"available": False, "reason": "未配置受控 Docker 端点，重启由宿主机管理。"}


def prepare_restart() -> tuple[Dict[str, Any], Optional[_RestartTicket]]:
    """Reserve the ops lock and prepare a restart without stopping this API."""
    lock_context = _operation_lock()
    lock_file = lock_context.__enter__()
    if lock_file is None:
        lock_context.__exit__(None, None, None)
        return _not_started("另一项运维操作正在执行，容器重启未发起"), None
    # Refuse to boot a mounted checkout against dependencies the running image lacks
    # — an import failure there would loop under `restart: unless-stopped`. Enforced
    # here so every restart path (ops page, config-save, version panel) is covered.
    try:
        needs_rebuild = rebuild_required()
    except (OSError, subprocess.TimeoutExpired):
        needs_rebuild = False
    if needs_rebuild:
        lock_context.__exit__(None, None, None)
        return _not_started(
            "依赖与运行镜像不一致，重启已被阻止：请先在宿主机 docker compose up -d --build，"
            "再重启以避免重启循环"), None
    capability = restart_capability()
    if not capability["available"]:
        lock_context.__exit__(None, None, None)
        return _not_started(capability["reason"]), None
    mode = capability.get("mode", "docker")
    scheduled_note = ("重启已排队：将在当前响应后退出进程，由容器重启策略自动拉起"
                      if mode == "self"
                      else "重启已排队，将在当前响应发送并完成审计后执行")
    result = {
        "started": True,
        "completed": False,
        "exit_code": None,
        "scheduled": True,
        "mode": mode,
        "output": [scheduled_note],
    }
    return result, _RestartTicket(lock_context, lock_file, mode)


def execute_restart(ticket: _RestartTicket) -> None:
    """Restart from a post-response background task, via Docker or a self-exit."""
    try:
        if ticket.mode == "self":
            _execute_self_restart()
            return
        result = _run_bounded_command(
            ["docker", "restart", "--timeout", "30", CONTAINER_NAME],
            cwd="/", timeout_seconds=RESTART_COMMAND_TIMEOUT_SECONDS,
            lock_file=ticket.lock_file)
        if result.get("exit_code") not in (0, None):
            get_logger().warning(
                f"Scheduled dashboard restart failed with exit code {result['exit_code']}")
    except Exception as e:
        get_logger().warning(f"Scheduled dashboard restart failed, error: {e}")
    finally:
        ticket.lock_context.__exit__(None, None, None)


def _execute_self_restart() -> None:
    """Gracefully terminate the gunicorn master (PID 1) so the policy respawns us.

    The container's `restart: unless-stopped` starts a fresh process that, under
    `preload_app`, re-imports the application from the mounted checkout — this is
    how a pulled code update actually takes effect. Needs no Docker socket.
    """
    get_logger().info("Dashboard self-restart: signaling gunicorn master (PID 1) to exit")
    try:
        os.kill(1, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError) as e:
        get_logger().warning(f"Dashboard self-restart could not signal PID 1, error: {e}")


def git_pull_capability() -> Dict[str, Any]:
    """Report whether an operator deliberately mounted a writable checkout."""
    if not REPO_DIR:
        return {
            "available": False,
            "reason": "标准部署由宿主机发布流程更新，面板内代码更新未启用。",
        }
    try:
        inspection = subprocess.run(
            ["git", "-C", REPO_DIR, "rev-parse", "--is-inside-work-tree"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            timeout=GIT_PREFLIGHT_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "reason": "受控 Git 工作区不可用。"}
    if inspection.returncode != 0 or inspection.stdout.strip() != "true":
        return {"available": False, "reason": "配置的代码目录不是可更新的 Git 工作区。"}
    return {"available": True, "reason": "已连接受控 Git 工作区，仅允许 fast-forward 更新。"}


def _compute_deps_fingerprint(base_dir: str) -> Optional[str]:
    """Hash the dependency-/build-defining files under base_dir.

    Returns None only when the comparison is genuinely inconclusive — the base
    directory is absent/unreadable, or a tracked file exists but cannot be read.
    A directory that exists but is missing every tracked file still yields a real
    digest (of the missing markers), so removing or relocating them all reads as a
    definite change rather than an inconclusive one. A missing file is folded into
    the hash as an explicit marker, so adding or removing one shifts the digest.
    """
    if not os.path.isdir(base_dir):
        return None
    digest = hashlib.sha256()
    for name in DEPS_FILES:
        path = os.path.join(base_dir, name)
        try:
            with open(path, "rb") as handle:
                content = handle.read()
            digest.update(name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(content)
            digest.update(b"\0")
        except (FileNotFoundError, NotADirectoryError):
            digest.update(name.encode("utf-8"))
            digest.update(b"\0<missing>\0")
        except OSError:
            return None
    return digest.hexdigest()


def rebuild_required() -> bool:
    """True when the mounted checkout's dependencies differ from the running image.

    Stateless by design: recomputed on every call, so it survives a panel reload,
    covers every restart entry point, and clears itself once a host rebuild bakes
    the new dependencies into the image.

    Only meaningful when a checkout is mounted (REPO_DIR set) — standard deployments
    never run pulled code, so they are never blocked. Within an opt-in deployment it
    fails closed: if the baked or checkout fingerprint cannot be read (e.g. an image
    built before this change is reused under a mounted checkout), it cannot prove the
    dependencies match, so it requires a rebuild rather than risk a restart loop.
    """
    if not REPO_DIR:
        return False
    baked = _compute_deps_fingerprint(DEPS_BAKED_DIR)
    checkout = _compute_deps_fingerprint(REPO_DIR)
    if baked is None or checkout is None:
        return True
    return baked != checkout


def git_pull() -> Dict[str, Any]:
    capability = git_pull_capability()
    if not capability["available"]:
        return _not_started(capability["reason"])
    with _operation_lock() as lock_file:
        if lock_file is None:
            return _not_started("另一项运维操作正在执行，git pull 未发起")
        try:
            result = _run_bounded_command(
                ["git", "-C", REPO_DIR, "pull", "--ff-only"],
                cwd=REPO_DIR, timeout_seconds=GIT_PULL_TIMEOUT_SECONDS,
                lock_file=lock_file)
        except OSError:
            return _not_started("git 或代码目录不可用，git pull 未发起")
        # The dependency check must never turn a completed pull into a failure:
        # any error here is reported conservatively as "rebuild required", not raised.
        try:
            result["dependencies_changed"] = rebuild_required()
        except (OSError, subprocess.TimeoutExpired):
            result["dependencies_changed"] = True
            result["output"].append("无法确认依赖是否变更，保守要求在宿主机重建镜像")
        if result.get("dependencies_changed"):
            result["output"].append(
                "检测到依赖与运行镜像不一致，仅重启不生效且会被阻止，"
                "需在宿主机执行 docker compose up -d --build")
        return result


def _git_text(args: List[str], timeout_seconds: int) -> tuple[int, str]:
    """Run one read-only git command in the checkout, returning trimmed output."""
    proc = subprocess.run(
        ["git", "-C", REPO_DIR, *args], stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, timeout=timeout_seconds, check=False)
    return proc.returncode, (proc.stdout or "").strip()


def check_update() -> Dict[str, Any]:
    """Compare the local checkout against its tracked remote without touching it.

    Fetches the remote-tracking ref (network read only; never writes the working
    tree) so the version panel can decide whether a one-click update is worth
    offering. Runs under the shared ops lock so it never races an in-flight pull.
    """
    from pr_agent.dashboard.version import get_app_version
    version = get_app_version()
    capability = git_pull_capability()
    result: Dict[str, Any] = {
        "version": version,
        "available": capability["available"],
        "reason": capability["reason"],
        "checked": False,
        "current": None,
        "latest": None,
        "behind": None,
        "update_available": False,
    }
    if not capability["available"]:
        return result
    with _operation_lock() as lock_file:
        if lock_file is None:
            result["reason"] = "另一项运维操作正在执行，暂时无法检查更新"
            return result
        try:
            head_rc, head_sha = _git_text(
                ["rev-parse", "--short", "HEAD"], GIT_PREFLIGHT_TIMEOUT_SECONDS)
            _, head_subject = _git_text(
                ["log", "-1", "--format=%s"], GIT_PREFLIGHT_TIMEOUT_SECONDS)
            result["current"] = {
                "sha": head_sha if head_rc == 0 else None,
                "subject": head_subject or None,
            }
            upstream_rc, upstream = _git_text(
                ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
                GIT_PREFLIGHT_TIMEOUT_SECONDS)
            if upstream_rc != 0 or not upstream:
                result["reason"] = "当前分支未跟踪远端分支，无法检查更新。"
                return result
            fetch_rc, fetch_out = _git_text(["fetch", "--quiet"], GIT_FETCH_TIMEOUT_SECONDS)
            if fetch_rc != 0:
                result["reason"] = ("拉取远端信息失败：" + (fetch_out or "git fetch 未成功"))[:300]
                return result
            # Every comparison must succeed before claiming a verdict: after a fetch
            # (which may prune a deleted upstream branch) a failed rev-parse/rev-list
            # must not be silently rendered as "already latest".
            latest_rc, latest_sha = _git_text(
                ["rev-parse", "--short", "@{u}"], GIT_PREFLIGHT_TIMEOUT_SECONDS)
            if latest_rc != 0 or not latest_sha:
                result["reason"] = "无法解析远端版本，上游分支可能已被删除或重命名。"
                return result
            _, latest_subject = _git_text(
                ["log", "-1", "--format=%s", "@{u}"], GIT_PREFLIGHT_TIMEOUT_SECONDS)
            result["latest"] = {
                "sha": latest_sha,
                "subject": latest_subject or None,
                "branch": upstream,
            }
            behind_rc, behind = _git_text(
                ["rev-list", "--count", "HEAD..@{u}"], GIT_PREFLIGHT_TIMEOUT_SECONDS)
            if behind_rc != 0 or not behind.isdigit():
                result["reason"] = "无法比较与远端的差异，暂时无法确认更新。"
                return result
            result["behind"] = int(behind)
            result["update_available"] = int(behind) > 0
            result["checked"] = True
            return result
        except (OSError, subprocess.TimeoutExpired) as e:
            result["reason"] = ("检查更新失败：" + str(e))[:300]
            return result


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


def probe_github_app(timeout_seconds: float = 30) -> Dict[str, Any]:
    """Validate the App JWT, an active installation, and repository access."""
    from pr_agent.config_loader import get_settings
    settings = get_settings()
    app_id = str(settings.get("github.app_id", "")).strip()
    private_key = str(settings.get("github.private_key", "")).strip()
    if not app_id or not private_key:
        return {"ok": False, "error": "github.app_id / github.private_key are not configured"}
    timeout_seconds = max(0.0, float(timeout_seconds))
    deadline = time.monotonic() + timeout_seconds

    def request_timeout() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"GitHub App probe exceeded its {timeout_seconds:g}s deadline")
        return min(15.0, remaining)

    try:
        import jwt
        now = int(time.time())
        payload = {"iat": now, "exp": now + 600, "iss": app_id}
        app_token = jwt.encode(payload, private_key, algorithm="RS256")
        import requests
        app_headers = {"Authorization": f"Bearer {app_token}",
                       "Accept": "application/vnd.github+json"}
        app_response = requests.get(
            "https://api.github.com/app", timeout=request_timeout(), headers=app_headers)
        if app_response.status_code != 200:
            return {"ok": False, "app_id": app_id,
                    "error": f"GitHub App API returned {app_response.status_code}"}
        active_installations = []
        installations_url = "https://api.github.com/app/installations?per_page=100"
        while installations_url:
            installations_response = requests.get(
                installations_url, timeout=request_timeout(), headers=app_headers)
            if installations_response.status_code != 200:
                return {"ok": False, "app_id": app_id,
                        "error": f"GitHub installations API returned {installations_response.status_code}"}
            installations = installations_response.json()
            active_installations.extend(
                item for item in installations if not item.get("suspended_at"))
            installations_url = _next_link(
                getattr(installations_response, "headers", {}).get("Link", ""))
        if not active_installations:
            return {"ok": False, "app_id": app_id, "error": "no active GitHub App installation"}

        probe_errors = []
        for installation in active_installations:
            installation_id = int(installation["id"])
            token_response = requests.post(
                f"https://api.github.com/app/installations/{installation_id}/access_tokens",
                timeout=request_timeout(), headers=app_headers)
            if token_response.status_code != 201:
                probe_errors.append(
                    f"installation {installation_id} token API returned {token_response.status_code}")
                continue
            installation_token = str(token_response.json().get("token", ""))
            if not installation_token:
                probe_errors.append(f"installation {installation_id} token response was empty")
                continue
            installation_headers = {
                "Authorization": f"Bearer {installation_token}",
                "Accept": "application/vnd.github+json",
            }
            try:
                repositories_response = requests.get(
                    "https://api.github.com/installation/repositories?per_page=1",
                    timeout=request_timeout(), headers=installation_headers)
                if repositories_response.status_code != 200:
                    probe_errors.append(
                        f"installation {installation_id} repository access returned "
                        f"{repositories_response.status_code}")
                    continue
                repository_count = int(repositories_response.json().get("total_count", 0))
                if repository_count <= 0:
                    probe_errors.append(f"installation {installation_id} has no accessible repositories")
                    continue
                return {
                    "ok": True,
                    "app_id": app_id,
                    "app_name": app_response.json().get("name", ""),
                    "installation_id": installation_id,
                    "repository_count": repository_count,
                }
            finally:
                try:
                    revocation = requests.delete(
                        "https://api.github.com/installation/token",
                        timeout=TOKEN_REVOCATION_TIMEOUT_SECONDS, headers=installation_headers)
                    # GitHub answers 204; anything else means the repository-access
                    # token is still live for up to an hour, so say so out loud
                    # rather than letting a successful probe imply it was revoked.
                    status = getattr(revocation, "status_code", None)
                    if status != 204:
                        get_logger().warning(
                            f"GitHub probe token revocation returned {status}; the temporary "
                            f"installation token stays valid until it expires")
                except Exception as e:
                    get_logger().warning(f"GitHub probe token revocation failed, error: {e}")
        return {
            "ok": False,
            "app_id": app_id,
            "app_name": app_response.json().get("name", ""),
            "installation_count": len(active_installations),
            "error": "; ".join(probe_errors)[:300] or "no accessible GitHub repositories",
        }
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
