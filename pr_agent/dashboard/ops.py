"""One-click operations and self-diagnosis for the dashboard.

Ops commands are restricted to a fixed whitelist - there is no generic shell
endpoint. Updates are prepared in an isolated Git worktree by an API worker.
Self-restart is prepared under the shared lock, then executed as a FastAPI
post-response task so the acknowledgment and audit reach the client first.
Probes (LLM relay, GitHub App credential, storage) never raise.
"""

import asyncio
import fcntl
import hashlib
import os
import shutil
import signal
import subprocess
import threading
import time
from contextlib import contextmanager, suppress
from typing import Any, Dict, List, Optional

from pr_agent.log import get_logger

# Fixed commands only; nothing constructed from user input reaches the shell.
CONTAINER_NAME = os.environ.get("ACHORD_REVIEW_CONTAINER", "achord-review")
REPO_DIR = os.environ.get("ACHORD_REVIEW_REPO_DIR", "").strip()
RELEASES_DIR = os.environ.get("ACHORD_REVIEW_RELEASES_DIR", "").strip()
UPDATE_REF = os.environ.get("ACHORD_REVIEW_UPDATE_REF", "@{u}").strip() or "@{u}"
CONFIG_DIR = os.environ.get("ACHORD_REVIEW_CONFIG_DIR", "").strip()
# The mounted source checkout that owns the Git metadata for every release
# worktree. The launcher stages the image's own revision from it on boot.
SOURCE_DIR = os.environ.get("ACHORD_REVIEW_SOURCE_DIR", "").strip()
# Superseded releases kept for a host-side rollback; older worktrees are removed
# at boot so the persistent releases volume stays bounded.
RELEASE_RETENTION = 1
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
DEPS_FILES = (
    "requirements.txt",
    "pyproject.toml",
    "docker/Dockerfile",
    "deploy/achord-review/docker-compose.yml",
    "deploy/achord-review/run-staged-release.sh",
    # The boot reconciler runs from the image's baked copy, not the staged release
    # (the launcher imports it before switching `current`). So a staged update that
    # only changes reconciliation would be declared restart-safe while the restart
    # still executes the old logic. Fingerprint it too, so any change to it requires
    # a host rebuild that actually bakes the new reconciler in.
    "pr_agent/dashboard/ops.py",
)
DEPS_BAKED_DIR = os.environ.get("ACHORD_DEPS_BAKED_DIR", "/app/.deps-baked")
# A per-build stamp written by the Dockerfile after the code is added. A stamp the
# launcher has not seen before means the operator rebuilt the image, and the
# rebuilt source checkout - not any earlier staged release - is what must run.
IMAGE_BUILD_ID_FILE = "build-id"
IMAGE_BUILD_MARKER = ".image-build-id"
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

    def __init__(self, lock_context, lock_file, mode: str = "docker",
                 pending: Optional[str] = None):
        self.lock_context = lock_context
        self.lock_file = lock_file
        self.mode = mode
        self.pending = pending


def _self_restart_capability() -> Optional[Dict[str, Any]]:
    """Socket-free fallback: exit PID 1 and let the restart policy respawn us."""
    if not SELF_RESTART_ENABLED:
        return None
    return {"available": True, "mode": "self",
            "reason": "将通过退出进程、由容器重启策略自动拉起（无需 Docker 端点）。"}


def _own_container_id() -> str:
    """Best-effort id of the container this process runs in, for self-verification.

    Docker records the full 64-hex container id in the cgroup and mount paths, and
    (unless overridden) uses its first 12 chars as the hostname. Any of these lets
    a `docker inspect` result be matched against the running container.
    """
    for source in ("/proc/self/mountinfo", "/proc/self/cgroup"):
        try:
            with open(source, encoding="utf-8") as handle:
                blob = handle.read()
        except OSError:
            continue
        for token in blob.replace("/", " ").replace("-", " ").split():
            candidate = token.strip()
            if len(candidate) == 64 and all(c in "0123456789abcdef" for c in candidate.lower()):
                return candidate.lower()
    return (os.environ.get("HOSTNAME", "") or "").strip().lower()


def _docker_target_is_self() -> Optional[bool]:
    """Whether `docker inspect CONTAINER_NAME` names this very container.

    Returns True/False when it can be decided, or None when the Docker endpoint is
    unusable (so the caller falls back rather than treating it as a mismatch).
    """
    try:
        inspection = subprocess.run(
            ["docker", "inspect", "-f", "{{.Id}}", CONTAINER_NAME], stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, timeout=DOCKER_PREFLIGHT_TIMEOUT_SECONDS,
            check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if inspection.returncode != 0:
        return None
    target = (inspection.stdout or "").strip().lower()
    own = _own_container_id()
    if not target or not own:
        return False
    return target.startswith(own) or own.startswith(target)


def restart_capability() -> Dict[str, Any]:
    """Report how a restart can happen: a Docker endpoint, or a socket-free self-exit.

    Docker mode is offered only when the inspected `CONTAINER_NAME` is verified to be
    this running container — a stale or mistargeted name must never let a restart hit
    an unrelated container after `current` has already been switched.
    """
    target_is_self = _docker_target_is_self()
    if target_is_self is True:
        return {"available": True, "mode": "docker",
                "reason": "已连接受控 Docker 端点，重启将在响应后执行。"}
    # Docker endpoint absent, unusable, or naming another container — fall back.
    self_restart = _self_restart_capability()
    if self_restart is not None:
        return self_restart
    if target_is_self is False:
        return {"available": False,
                "reason": (f"受控容器名 {CONTAINER_NAME} 指向的不是当前服务容器，"
                           "重启已禁用；请核对 ACHORD_REVIEW_CONTAINER 或启用自重启。")}
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
            "依赖与运行镜像不一致，重启已被阻止：请先在宿主机执行 "
            "git pull --ff-only && docker compose up -d --build，以避免重启循环"), None
    capability = restart_capability()
    if not capability["available"]:
        lock_context.__exit__(None, None, None)
        return _not_started(capability["reason"]), None
    # The switch itself happens in execute_restart, immediately before the restart is
    # initiated, so a restart that fails to start can roll it back. Only the
    # preconditions are checked here, where a failure can still be reported.
    pending = _pending_release()
    if pending is not None and not os.path.islink(REPO_DIR.rstrip(os.sep)):
        lock_context.__exit__(None, None, None)
        return _not_started("活动代码路径不是可原子切换的符号链接，重启未发起"), None
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
    if pending:
        result["output"].insert(
            0, f"重启发起时将原子切换到待发布版本 {os.path.basename(pending)[:12]}")
    return result, _RestartTicket(lock_context, lock_file, mode, pending=pending)


def execute_restart(ticket: _RestartTicket) -> None:
    """Restart from a post-response background task, via Docker or a self-exit.

    A pending release is switched in only here, right before the restart is
    initiated. If initiation fails synchronously (Docker refuses, PID 1 cannot be
    signaled), the previous target and the pending marker are restored so the
    still-serving old process and the `current` link never disagree.
    """
    activation: Optional[_Activation] = None
    try:
        if ticket.pending:
            activation = _activate_pending_release()
        if ticket.mode == "self":
            initiated = _execute_self_restart()
        else:
            result = _run_bounded_command(
                ["docker", "restart", "--timeout", "30", CONTAINER_NAME],
                cwd="/", timeout_seconds=RESTART_COMMAND_TIMEOUT_SECONDS,
                lock_file=ticket.lock_file)
            initiated = result.get("exit_code") == 0
            if not initiated:
                get_logger().warning(
                    f"Scheduled dashboard restart failed with exit code {result.get('exit_code')}")
        if not initiated and activation is not None:
            _rollback_activation(activation)
    except Exception as e:
        get_logger().warning(f"Scheduled dashboard restart failed, error: {e}")
        if activation is not None:
            _rollback_activation(activation)
    finally:
        ticket.lock_context.__exit__(None, None, None)


def _execute_self_restart() -> bool:
    """Gracefully terminate the gunicorn master (PID 1) so the policy respawns us.

    The container's `restart: unless-stopped` starts a fresh process that, under
    `preload_app`, re-imports the application from the mounted checkout — this is
    how a pulled code update actually takes effect. Needs no Docker socket.
    Returns whether the signal was delivered.
    """
    get_logger().info("Dashboard self-restart: signaling gunicorn master (PID 1) to exit")
    try:
        os.kill(1, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError) as e:
        get_logger().warning(f"Dashboard self-restart could not signal PID 1, error: {e}")
        return False
    return True


def git_pull_capability() -> Dict[str, Any]:
    """Report whether an operator configured isolated staged releases.

    The earlier in-place mode (only ACHORD_REVIEW_REPO_DIR set, `git pull` into
    the running checkout) is deliberately retired: it mutated live Python and
    static files under a serving process. A deployment still configured that way
    is told exactly what to add rather than silently losing the button.
    """
    if not REPO_DIR:
        return {
            "available": False,
            "reason": "标准部署由宿主机发布流程更新，面板内分阶段更新未启用。",
        }
    if not RELEASES_DIR:
        return {
            "available": False,
            "reason": ("旧版就地 git pull 模式已移除：请按 README「Migrating from in-place updates」"
                       "启用分阶段发布（启动器 command、/app/source 与 releases/config 挂载、"
                       "ACHORD_REVIEW_RELEASES_DIR/SOURCE_DIR/CONFIG_DIR 环境变量）。"),
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
    try:
        os.makedirs(RELEASES_DIR, mode=0o700, exist_ok=True)
    except OSError:
        return {"available": False, "reason": "受控发布目录不可写。"}
    # A staged release only inherits the production settings and credentials
    # through this link; without it a switch would boot an unconfigured service.
    if not CONFIG_DIR or not os.path.isdir(CONFIG_DIR):
        return {"available": False,
                "reason": "未配置持久化配置目录（ACHORD_REVIEW_CONFIG_DIR），分阶段更新未启用。"}
    try:
        update_ref, reason = _resolve_update_ref()
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "reason": "无法解析更新所跟踪的远端分支。"}
    if update_ref is None:
        return {"available": False, "reason": reason}
    return {"available": True, "reason": "已连接受控 Git 工作区，更新会先写入独立发布目录。"}


def _resolve_update_ref() -> tuple[Optional[str], str]:
    """Return the concrete remote-tracking ref updates compare against.

    `current` is always a detached worktree, so the default `@{u}` cannot be
    resolved there; it is resolved in the source checkout (whose branch carries
    the upstream) and shared through the common Git metadata. Returns
    (ref, reason) with ref None when nothing usable can be determined.
    """
    if UPDATE_REF != "@{u}":
        return UPDATE_REF, ""
    upstream_args = ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]
    if SOURCE_DIR:
        upstream_rc, resolved = _git_text_at(SOURCE_DIR, upstream_args, GIT_PREFLIGHT_TIMEOUT_SECONDS)
    else:
        upstream_rc, resolved = _git_text(upstream_args, GIT_PREFLIGHT_TIMEOUT_SECONDS)
    if upstream_rc != 0 or not resolved or resolved == "@{u}":
        return None, "源码检出的分支未跟踪远端分支，请设置 ACHORD_REVIEW_UPDATE_REF。"
    return resolved, ""


def _remote_of_ref(ref: str) -> Optional[str]:
    """The remote a `<remote>/<branch>` tracking ref names, or None if it has none."""
    return ref.split("/", 1)[0] if ref and "/" in ref else None


def _fetch_args_for(ref: str) -> List[str]:
    """Fetch exactly the remote backing the comparison ref, not Git's default one.

    In a checkout with several remotes, a bare `git fetch` updates `origin` while
    the comparison may use e.g. `upstream/main`, leaving that ref stale. Naming the
    ref's own remote keeps the fetched objects and the comparison in agreement.
    """
    remote = _remote_of_ref(ref)
    return ["fetch", "--quiet", remote] if remote else ["fetch", "--quiet"]


def _pending_marker_path() -> str:
    return os.path.join(RELEASES_DIR, ".pending-release")


def _safe_release_path(path: str) -> Optional[str]:
    """Return a canonical release path only when it is contained by RELEASES_DIR."""
    if not RELEASES_DIR or not path:
        return None
    releases = os.path.realpath(RELEASES_DIR)
    candidate = os.path.realpath(path)
    try:
        if os.path.commonpath((releases, candidate)) != releases:
            return None
    except ValueError:
        return None
    return candidate if candidate != releases else None


def _pending_release() -> Optional[str]:
    try:
        with open(_pending_marker_path(), encoding="utf-8") as handle:
            candidate = handle.read().strip()
    except (OSError, ValueError):
        return None
    candidate = _safe_release_path(candidate)
    return candidate if candidate and os.path.isdir(candidate) else None


def _write_pending_release(path: str) -> None:
    """Persist a fully prepared release using an atomic marker replacement."""
    marker = _pending_marker_path()
    temporary = f"{marker}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(path + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, marker)


def _ensure_release_config_link(release_path: str) -> None:
    """Attach the stable deployment config without exposing it to Git."""
    if not CONFIG_DIR or not os.path.isdir(CONFIG_DIR):
        raise OSError("未配置持久化配置目录，无法为发布版本挂接 settings_prod")
    settings_path = os.path.join(release_path, "pr_agent", "settings_prod")
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    if os.path.lexists(settings_path):
        if (os.path.islink(settings_path)
                and os.path.realpath(settings_path) == os.path.realpath(CONFIG_DIR)):
            return
        raise OSError("待发布版本的 settings_prod 路径不是预期配置链接")
    os.symlink(CONFIG_DIR, settings_path)


class _Activation:
    """What an atomic switch replaced, so a failed restart can undo it."""

    def __init__(self, release: str, previous: Optional[str], was_pending: bool):
        self.release = release
        self.previous = previous
        self.was_pending = was_pending


def _switch_active_release(target: str) -> Optional[str]:
    """Atomically point the stable current symlink at target; return the old target."""
    active_link = REPO_DIR.rstrip(os.sep)
    previous = os.readlink(active_link) if os.path.islink(active_link) else None
    if previous is None and os.path.lexists(active_link):
        raise OSError("活动代码路径不是可原子切换的符号链接")
    temporary_link = f"{active_link}.{os.getpid()}.tmp"
    try:
        os.symlink(target, temporary_link)
        os.replace(temporary_link, active_link)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary_link)
    return previous


def _activate_pending_release() -> Optional[_Activation]:
    """Atomically redirect the stable current symlink to the prepared worktree."""
    pending = _pending_release()
    if pending is None:
        return None
    previous = _switch_active_release(pending)
    activation = _Activation(pending, previous, was_pending=True)
    try:
        os.unlink(_pending_marker_path())
    except OSError:
        # The marker is still in place, so only the link has to be undone before
        # surfacing the error; `current` must never outrun the process serving it.
        if previous is not None:
            _switch_active_release(previous)
        raise
    return activation


def _rollback_activation(activation: _Activation) -> None:
    """Restore the previous target and pending marker after a failed restart."""
    try:
        if activation.previous is not None:
            _switch_active_release(activation.previous)
        if activation.was_pending:
            _write_pending_release(activation.release)
        get_logger().warning(
            f"Dashboard restart did not start; rolled `current` back to {activation.previous}")
    except OSError as e:
        get_logger().error(f"Dashboard release rollback failed, error: {e}")


def _release_admin_dir() -> str:
    """The checkout whose Git metadata registers every release worktree."""
    return SOURCE_DIR or REPO_DIR


def _discard_release_worktree(release_path: str) -> None:
    """Remove a worktree and its registration so the same revision can be retried."""
    admin = _release_admin_dir()
    with suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            ["git", "-C", admin, "worktree", "remove", "--force", release_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=GIT_PREFLIGHT_TIMEOUT_SECONDS, check=False)
    shutil.rmtree(release_path, ignore_errors=True)
    with suppress(OSError, subprocess.TimeoutExpired):
        # `--expire now` is required: without it Git keeps a just-missing worktree
        # registered until its expiry window, so a retry of the same revision fails
        # as already registered even though the directory is gone.
        subprocess.run(
            ["git", "-C", admin, "worktree", "prune", "--expire", "now"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=GIT_PREFLIGHT_TIMEOUT_SECONDS, check=False)


def _is_revision(name: str) -> bool:
    return len(name) == 40 and all(c in "0123456789abcdef" for c in name.lower())


def _release_complete_marker(release_path: str) -> str:
    return os.path.join(release_path, ".release-complete")


def _release_is_complete(release_path: str) -> bool:
    """Whether a release directory finished its checkout, not just its registration."""
    return os.path.exists(_release_complete_marker(release_path))


def _mark_release_complete(release_path: str) -> None:
    """Record completion only after a full checkout, so a partial one is never reused."""
    marker = _release_complete_marker(release_path)
    with open(f"{marker}.tmp", "w", encoding="utf-8") as handle:
        handle.write("ok\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(f"{marker}.tmp", marker)


def _ensure_release_worktree(revision: str) -> str:
    """Return the release directory for revision, creating the worktree if needed.

    A directory is reused only when its checkout actually finished (a completion
    marker written after `git worktree add`), not merely because Git registered it
    and `rev-parse` resolves. An interrupted or wrong-revision directory is discarded
    and rebuilt, so a half-written worktree is never activated.
    """
    release_path = os.path.join(RELEASES_DIR, revision)
    if os.path.isdir(release_path):
        rc, existing = _git_text_at(release_path, ["rev-parse", "HEAD"], GIT_PREFLIGHT_TIMEOUT_SECONDS)
        if rc == 0 and existing == revision and _release_is_complete(release_path):
            _ensure_release_config_link(release_path)
            return release_path
        _discard_release_worktree(release_path)
    proc = subprocess.run(
        ["git", "-C", _release_admin_dir(), "worktree", "add", "--detach", release_path, revision],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        timeout=GIT_PULL_TIMEOUT_SECONDS, check=False)
    if proc.returncode != 0:
        _discard_release_worktree(release_path)
        raise OSError(f"无法准备发布版本 {revision[:12]}：{(proc.stdout or '').strip()[:200]}")
    _ensure_release_config_link(release_path)
    _mark_release_complete(release_path)
    return release_path


def _read_marker(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip() or None
    except OSError:
        return None


def prune_releases(keep: Optional[str] = None) -> List[str]:
    """Remove superseded release worktrees beyond RELEASE_RETENTION.

    The active target, the pending release and `keep` are never removed; the
    newest RELEASE_RETENTION other releases stay for a host-side rollback. Only
    called at boot, when no process is serving from a superseded worktree.
    """
    protected = {p for p in (
        os.path.realpath(REPO_DIR) if os.path.islink(REPO_DIR.rstrip(os.sep)) else None,
        _pending_release(), keep and os.path.realpath(keep)) if p}
    candidates = []
    for name in os.listdir(RELEASES_DIR):
        path = os.path.join(RELEASES_DIR, name)
        if not _is_revision(name) or not os.path.isdir(path) or os.path.realpath(path) in protected:
            continue
        candidates.append((os.path.getmtime(path), path))
    candidates.sort(reverse=True)
    removed = []
    for _, path in candidates[RELEASE_RETENTION:]:
        _discard_release_worktree(path)
        removed.append(path)
    return removed


def reconcile_boot_release() -> str:
    """Decide which release the launcher boots and make `current` point at it.

    Runs before Gunicorn in the release launcher. Priority order:
    1. A rebuilt image (fresh build stamp) runs the source checkout's HEAD - the
       revision it was built from - and discards any older pending release, so a
       host `git pull --ff-only && docker compose up -d --build` always takes
       effect even without a marker and never boots a stale staged revision.
    2. Otherwise a pending release whose dependencies match the image is
       activated (a dependency-changing update left it waiting for this rebuild).
    3. Otherwise the existing `current` target keeps running.
    Returns the activated release path.
    """
    if not (REPO_DIR and RELEASES_DIR and SOURCE_DIR):
        raise OSError("release launcher requires REPO_DIR, RELEASES_DIR and SOURCE_DIR")
    os.makedirs(RELEASES_DIR, mode=0o700, exist_ok=True)
    active_link = REPO_DIR.rstrip(os.sep)
    build_id = _read_marker(os.path.join(DEPS_BAKED_DIR, IMAGE_BUILD_ID_FILE))
    seen_build_id = _read_marker(os.path.join(RELEASES_DIR, IMAGE_BUILD_MARKER))
    rebuilt = build_id is not None and build_id != seen_build_id
    if rebuilt or not os.path.islink(active_link):
        rc, source_head = _git_text_at(SOURCE_DIR, ["rev-parse", "HEAD"], GIT_PREFLIGHT_TIMEOUT_SECONDS)
        if rc != 0 or not _is_revision(source_head):
            raise OSError("无法解析源码检出的 HEAD，发布启动器无法确定要运行的版本")
        release = _ensure_release_worktree(source_head)
        # A fresh build stamp is not on its own proof the image was built from this
        # HEAD: the checkout may have advanced to a dependency-changing commit after
        # the build. Verify the release's dependency fingerprint against the image's
        # baked copy before booting it, or Gunicorn would run new code against the
        # image's old packages and loop under `restart: unless-stopped`.
        baked_fp = _compute_deps_fingerprint(DEPS_BAKED_DIR)
        release_fp = _compute_deps_fingerprint(release)
        if baked_fp is not None and release_fp is not None and baked_fp == release_fp:
            _switch_active_release(release)
            pending = _pending_release()
            if pending is not None and os.path.realpath(pending) != os.path.realpath(release):
                get_logger().warning(
                    f"Discarding pending release {os.path.basename(pending)[:12]}: the image was "
                    f"rebuilt from {source_head[:12]}")
            with suppress(FileNotFoundError):
                os.unlink(_pending_marker_path())
            if build_id is not None:
                marker = os.path.join(RELEASES_DIR, IMAGE_BUILD_MARKER)
                with open(f"{marker}.tmp", "w", encoding="utf-8") as handle:
                    handle.write(build_id + "\n")
                os.replace(f"{marker}.tmp", marker)
        elif not os.path.islink(active_link):
            # First boot with nothing already serving: fail closed rather than run a
            # checkout whose dependencies the image lacks.
            raise OSError(
                f"源码 HEAD {source_head[:12]} 的依赖与镜像烘焙的不一致，"
                "请从当前检出重建镜像（docker compose up -d --build）后再启动")
        else:
            # A rebuilt image whose baked dependencies do not match the mounted HEAD:
            # keep the running release, leave the build stamp unconsumed so a corrected
            # rebuild is still detected, and stage HEAD for that rebuild.
            get_logger().warning(
                f"Rebuilt image dependencies do not match source HEAD {source_head[:12]}; "
                "keeping the current release and staging HEAD as pending until a matching rebuild")
            _write_pending_release(release)
    elif _pending_release() is not None and not rebuild_required():
        _activate_pending_release()
    active = os.path.realpath(active_link)
    _ensure_release_config_link(active)
    prune_releases()
    return active


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
    # A staged release is what the next restart would boot. Compare that tree,
    # not the still-running release, so dependency-changing updates are blocked
    # before the atomic switch.
    checkout = _compute_deps_fingerprint(_pending_release() or REPO_DIR)
    if baked is None or checkout is None:
        return True
    return baked != checkout


def git_pull() -> Dict[str, Any]:
    """Fetch and prepare an isolated worktree without touching the live release."""
    capability = git_pull_capability()
    if not capability["available"]:
        return _not_started(capability["reason"])
    with _operation_lock() as lock_file:
        if lock_file is None:
            return _not_started("另一项运维操作正在执行，git pull 未发起")
        try:
            os.makedirs(RELEASES_DIR, mode=0o700, exist_ok=True)
            update_ref, unresolved = _resolve_update_ref()
            if update_ref is None:
                return _not_started(unresolved)
            fetch_rc, fetch_out = _git_text(_fetch_args_for(update_ref), GIT_FETCH_TIMEOUT_SECONDS)
            if fetch_rc != 0:
                return _not_started(("拉取远端信息失败：" + (fetch_out or "git fetch 未成功"))[:300])
            revision_rc, revision = _git_text(["rev-parse", update_ref], GIT_PREFLIGHT_TIMEOUT_SECONDS)
            if revision_rc != 0 or not _is_revision(revision):
                return _not_started("无法解析待发布的远端版本")
            ancestor_rc, _ = _git_text(
                ["merge-base", "--is-ancestor", "HEAD", revision], GIT_PREFLIGHT_TIMEOUT_SECONDS)
            if ancestor_rc != 0:
                return _not_started("当前版本与远端不满足 fast-forward 条件，请在宿主机处理分叉")
            release_path = os.path.join(RELEASES_DIR, revision)
            reuse = False
            if os.path.isdir(release_path):
                existing_rc, existing_revision = _git_text_at(
                    release_path, ["rev-parse", "HEAD"], GIT_PREFLIGHT_TIMEOUT_SECONDS)
                reuse = (existing_rc == 0 and existing_revision == revision
                         and _release_is_complete(release_path))
                if not reuse:
                    # A wrong-revision or interrupted/partial checkout: rebuild it
                    # cleanly instead of trusting a half-written worktree.
                    _discard_release_worktree(release_path)
            if reuse:
                _ensure_release_config_link(release_path)
                result = {"started": True, "completed": True, "exit_code": 0,
                          "timed_out": False, "output": ["远端版本已在独立发布目录中准备完成"]}
            else:
                stage = _run_bounded_command(
                    ["git", "-C", REPO_DIR, "worktree", "add", "--detach", release_path, revision],
                    cwd=REPO_DIR, timeout_seconds=GIT_PULL_TIMEOUT_SECONDS, lock_file=lock_file)
                if stage.get("exit_code") != 0 or not stage.get("completed"):
                    # Drop the registration too, or a retry of the same revision
                    # fails on a path Git still considers an existing worktree.
                    _discard_release_worktree(release_path)
                    return stage
                _ensure_release_config_link(release_path)
                _mark_release_complete(release_path)
                result = stage
            _write_pending_release(release_path)
            result["mode"] = "staged"
            result["release"] = revision
            result["output"].append("更新已分阶段准备；运行中的后端与静态资源尚未改变")
        except (OSError, subprocess.TimeoutExpired) as e:
            return _not_started(f"git 或发布目录不可用，更新未准备：{e}")
        # The dependency check must never turn a completed pull into a failure:
        # any error here is reported conservatively as "rebuild required", not raised.
        try:
            result["dependencies_changed"] = rebuild_required()
        except (OSError, subprocess.TimeoutExpired):
            result["dependencies_changed"] = True
            result["output"].append("无法确认依赖是否变更，保守要求在宿主机重建镜像")
        if result.get("dependencies_changed"):
            result["output"].append(
                "待发布版本的依赖与运行镜像不一致，仅重启不生效且会被阻止，"
                "需在宿主机执行 git pull --ff-only && docker compose up -d --build")
        return result


def _git_text(args: List[str], timeout_seconds: int) -> tuple[int, str]:
    """Run one read-only git command in the checkout, returning trimmed output."""
    return _git_text_at(REPO_DIR, args, timeout_seconds)


def _git_text_at(repo_dir: str, args: List[str], timeout_seconds: int) -> tuple[int, str]:
    """Run one read-only git command in a specified controlled checkout."""
    proc = subprocess.run(
        ["git", "-C", repo_dir, *args], stdout=subprocess.PIPE,
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
        "ahead": None,
        "diverged": False,
        "update_available": False,
        "pending": None,
        "staged": False,
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
            comparison_ref, unresolved = _resolve_update_ref()
            if comparison_ref is None:
                result["reason"] = unresolved
                return result
            upstream = comparison_ref
            fetch_rc, fetch_out = _git_text(_fetch_args_for(comparison_ref), GIT_FETCH_TIMEOUT_SECONDS)
            if fetch_rc != 0:
                result["reason"] = ("拉取远端信息失败：" + (fetch_out or "git fetch 未成功"))[:300]
                return result
            # Every comparison must succeed before claiming a verdict: after a fetch
            # (which may prune a deleted upstream branch) a failed rev-parse/rev-list
            # must not be silently rendered as "already latest".
            latest_rc, latest_sha = _git_text(
                ["rev-parse", "--short", comparison_ref], GIT_PREFLIGHT_TIMEOUT_SECONDS)
            if latest_rc != 0 or not latest_sha:
                result["reason"] = "无法解析远端版本，上游分支可能已被删除或重命名。"
                return result
            _, latest_subject = _git_text(
                ["log", "-1", "--format=%s", comparison_ref], GIT_PREFLIGHT_TIMEOUT_SECONDS)
            result["latest"] = {
                "sha": latest_sha,
                "subject": latest_subject or None,
                "branch": upstream,
            }
            # Count both sides: a diverged branch has upstream commits we lack AND
            # local commits upstream lacks, so `git pull --ff-only` cannot succeed —
            # never advertise a one-click update that is bound to fail.
            behind_rc, behind = _git_text(
                ["rev-list", "--count", f"HEAD..{comparison_ref}"], GIT_PREFLIGHT_TIMEOUT_SECONDS)
            ahead_rc, ahead = _git_text(
                ["rev-list", "--count", f"{comparison_ref}..HEAD"], GIT_PREFLIGHT_TIMEOUT_SECONDS)
            if behind_rc != 0 or not behind.isdigit() or ahead_rc != 0 or not ahead.isdigit():
                result["reason"] = "无法比较与远端的差异，暂时无法确认更新。"
                return result
            result["behind"] = int(behind)
            result["ahead"] = int(ahead)
            if int(ahead) > 0 and int(behind) > 0:
                result["diverged"] = True
                result["reason"] = "本地与远端已分叉，无法 fast-forward 更新，请在宿主机处理。"
            else:
                result["update_available"] = int(behind) > 0
            # The live HEAD deliberately stays put until restart, so a prepared
            # release must be reported as such instead of re-offered as an update.
            pending = _pending_release()
            if pending is not None:
                pending_rc, pending_rev = _git_text_at(
                    pending, ["rev-parse", "HEAD"], GIT_PREFLIGHT_TIMEOUT_SECONDS)
                latest_rc, latest_full = _git_text(["rev-parse", comparison_ref], GIT_PREFLIGHT_TIMEOUT_SECONDS)
                _, pending_subject = _git_text_at(
                    pending, ["log", "-1", "--format=%s"], GIT_PREFLIGHT_TIMEOUT_SECONDS)
                if pending_rc == 0 and _is_revision(pending_rev):
                    result["pending"] = {
                        "sha": pending_rev[:7],
                        "subject": pending_subject or None,
                        "rebuild_required": rebuild_required(),
                    }
                    if latest_rc == 0 and pending_rev == latest_full:
                        result["staged"] = True
                        result["update_available"] = False
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
