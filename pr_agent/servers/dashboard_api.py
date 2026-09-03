"""Dashboard API: admin auth, live config, one-click ops, review history and playground.

Mounted by pr_agent/servers/github_app.py under /api/v1/dashboard. All routes
require the admin session except /auth/login; the storage layer is fail-safe
(failures degrade to logs, never to webhook errors).

Routes reserved for future phases return 501 with code COMING_SOON so the
front end can wire them up before the backing features ship.
"""

import asyncio
import hashlib
import hmac
import os
import secrets
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictBool

from pr_agent.config_loader import get_settings
from pr_agent.dashboard import ops
from pr_agent.dashboard.config_engine import get_config_engine
from pr_agent.dashboard.storage import DashboardStorageReadError, get_storage
from pr_agent.log import get_logger

router = APIRouter(prefix="/api/v1/dashboard")

SESSION_COOKIE = "dashboard_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60
# Number of trusted proxy hops in front of this service. The deployment sits
# behind exactly one nginx; only headers appended by those hops are consumed,
# so clients cannot rotate their X-Forwarded-For to evade the login lockout.
TRUSTED_PROXY_HOPS = int(os.environ.get("DASHBOARD_TRUSTED_PROXY_HOPS", "0"))
DASHBOARD_EXTERNAL_ORIGIN = os.environ.get("DASHBOARD_EXTERNAL_ORIGIN", "").strip()

# Session and lockout state is persisted in the dashboard SQLite database so
# login remains stable across gunicorn workers. Bearer tokens and source
# addresses enter the database only as one-way identifiers, never as credentials.
MAX_LOCKOUT_KEYS = 10_000
_password_sync_state = {
    "db_path": "", "password": None, "generation": None, "signature": None,
}
_password_sync_lock = threading.Lock()


def _storage_call(method_name: str, *args, **kwargs):
    """Resolve the storage singleton and method inside a worker thread."""
    return getattr(get_storage(), method_name)(*args, **kwargs)


async def _dashboard_storage_read(method_name: str, *args, **kwargs):
    """Run an admin data query and expose storage outages as a truthful 503."""
    try:
        return await asyncio.to_thread(_storage_call, method_name, *args, **kwargs)
    except DashboardStorageReadError as e:
        get_logger().warning(f"Dashboard data query unavailable, error: {e}")
        raise HTTPException(status_code=503, detail="审查数据存储暂不可用，请稍后重试") from e


def _admin_password() -> str:
    return _admin_password_snapshot()[0]


def _admin_password_snapshot() -> tuple[str, tuple | None]:
    for variable_name in ("DASHBOARD_ADMIN_PASSWORD", "DASHBOARD__ADMIN_PASSWORD"):
        password = os.environ.get(variable_name, "")
        if password:
            # The password itself is compared in constant time at every cache
            # and request boundary. The signature only identifies its source;
            # deriving another value from the password would create an
            # unnecessary offline password oracle.
            return password, ("environment", variable_name)
    return get_config_engine().admin_password_snapshot()


def _client_ip(request: Request) -> str:
    """Client address, consuming only the trusted proxy hops' XFF entries.

    nginx appends the address it observed to X-Forwarded-For, so with H
    trusted hops the real client is the H-th value counting from the RIGHT:
    chain[-1] is what the last hop saw, chain[-2] what the second-to-last
    saw, and so on. Anything further left was supplied by the client (or an
    earlier hop) and is never trusted. With zero hops the header is ignored
    entirely and the socket address is used.
    """
    hops = max(0, TRUSTED_PROXY_HOPS)
    if hops == 0:
        return request.client.host if request.client else ""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        chain = [part.strip() for part in forwarded.split(",") if part.strip()]
        if len(chain) >= hops:
            return chain[-hops]
    return request.client.host if request.client else ""


def _lockout_key(request: Request) -> str:
    return _client_ip(request) or "unknown"


def _credential_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _session_hash(token: str, password: Optional[str] = None) -> str:
    """Bind a persisted session to the currently configured admin password."""
    password = _admin_password() if password is None else password
    if not password:
        return ""
    return hmac.new(password.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()


def _sync_admin_password(password: str, signature=None) -> bool:
    if signature is None:
        return False
    storage = get_storage()
    with _password_sync_lock:
        cached_password = _password_sync_state["password"]
        shared_generation = storage.admin_password_generation()
        if (shared_generation is not None
                and storage.db_path == _password_sync_state["db_path"]
                and shared_generation == _password_sync_state["generation"]
                and signature == _password_sync_state["signature"]
                and isinstance(cached_password, str)
                and hmac.compare_digest(password.encode("utf-8"), cached_password.encode("utf-8"))):
            return True
        if not storage.sync_admin_password(password):
            return False
        shared_generation = storage.admin_password_generation()
        if shared_generation is None:
            return False
        _password_sync_state.update({
            "db_path": storage.db_path,
            "password": password,
            "generation": shared_generation,
            "signature": signature,
        })
        return True


def _sync_current_admin_password() -> bool:
    password, signature = _admin_password_snapshot()
    return _sync_admin_password(password, signature)


async def _create_session(verified_password: str) -> str:
    token = secrets.token_urlsafe(32)

    def _create() -> str:
        current_password, current_signature = _admin_password_snapshot()
        if not hmac.compare_digest(
                verified_password.encode("utf-8"), current_password.encode("utf-8")):
            return "password_changed"
        token_hash = _session_hash(token, verified_password)
        created = get_storage().create_session_for_password(
            token_hash,
            int(time.time()) + SESSION_TTL_SECONDS,
            current_password)
        after_password, after_signature = _admin_password_snapshot()
        if (current_signature != after_signature
                or not hmac.compare_digest(
                    current_password.encode("utf-8"), after_password.encode("utf-8"))):
            get_storage().revoke_session(token_hash)
            return "password_changed"
        return "created" if created else "storage_error"

    result = await asyncio.to_thread(_create)
    if result == "password_changed":
        raise HTTPException(status_code=401, detail="管理员密码已变更，请使用新密码重新登录")
    if result != "created":
        raise HTTPException(status_code=503, detail="会话存储暂不可用，请稍后重试")
    return token


def _session_valid(token: Optional[str]) -> bool:
    password, signature = _admin_password_snapshot()
    if not _sync_admin_password(password, signature):
        return False
    if not token or not password:
        return False
    token_hash = _session_hash(token, password)
    valid = bool(token_hash and get_storage().session_is_valid(token_hash))
    after_password, after_signature = _admin_password_snapshot()
    if (signature != after_signature
            or not hmac.compare_digest(password.encode("utf-8"), after_password.encode("utf-8"))):
        _sync_admin_password(after_password, after_signature)
        return False
    return valid


async def require_auth(request: Request, dashboard_session: Optional[str] = Cookie(None)) -> None:
    if not await asyncio.to_thread(_session_valid, dashboard_session):
        # also accept a bearer token for scripted access
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer ") and await asyncio.to_thread(_session_valid, auth[7:].strip()):
            return
        raise HTTPException(status_code=401, detail="Not authenticated")


def require_same_origin(request: Request) -> None:
    """CSRF guard for cookie-authenticated mutations.

    The session cookie is SameSite=Lax, which still rides along on
    top-level form posts from a same-site sibling origin. Browsers that
    implement Fetch Metadata send Sec-Fetch-Site on every request; when the
    header is present it must say same-origin. When it is absent (older
    browser or scripted client) fall back to an exact Origin/Referer match.
    Browser requests using either supported credential still provide this
    origin evidence; non-browser bearer clients can send an explicit Origin.
    """
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site:
        if fetch_site != "same-origin":
            raise HTTPException(status_code=403, detail="Cross-site request rejected")
        return
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    if not origin:
        raise HTTPException(status_code=403, detail="Missing origin evidence")
    expected_origin = DASHBOARD_EXTERNAL_ORIGIN
    if not expected_origin:
        raise HTTPException(status_code=403, detail="Trusted external origin is not configured")
    actual_identity = _normalized_origin(origin)
    expected_identity = _normalized_origin(expected_origin)
    if actual_identity is None or expected_identity is None or actual_identity != expected_identity:
        raise HTTPException(status_code=403, detail="Cross-site request rejected")


def _normalized_origin(value: str) -> Optional[tuple[str, str, int]]:
    """Return the scheme/host/effective-port identity used by browser origins."""
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https") or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        port = parsed.port or (443 if scheme == "https" else 80)
        return scheme, parsed.hostname.lower(), port
    except ValueError:
        return None


def _ok(data: Any = None, message: str = "操作成功") -> Dict[str, Any]:
    return {"success": True, "data": data, "message": message}


def coming_soon() -> JSONResponse:
    return JSONResponse(status_code=501,
                        content={"success": False, "code": "COMING_SOON",
                                 "message": "该功能在规划中，尚未上线"})


# --------------------------------------------------------------------- auth

class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


@router.post("/auth/login")
async def auth_login(body: LoginRequest, request: Request, response: Response):
    key = _lockout_key(request)
    expected, _ = await asyncio.to_thread(_admin_password_snapshot)
    if not expected:
        await asyncio.to_thread(_sync_current_admin_password)
        raise HTTPException(status_code=503, detail="管理员密码未配置（config.toml [dashboard] admin_password）")
    password_matches = hmac.compare_digest(body.password.encode("utf-8"), expected.encode("utf-8"))
    decision = await asyncio.to_thread(
        _storage_call, "verify_login_attempt",
        _credential_hash(key), password_matches, time.time(), LOCKOUT_SECONDS,
        MAX_FAILED_ATTEMPTS, MAX_LOCKOUT_KEYS * MAX_FAILED_ATTEMPTS)
    if decision["storage_error"]:
        raise HTTPException(status_code=503, detail="登录保护存储暂不可用，请稍后重试")
    if decision["locked_out"]:
        raise HTTPException(status_code=429, detail="尝试次数过多，请 15 分钟后再试")
    if not decision["authenticated"]:
        remaining = max(0, MAX_FAILED_ATTEMPTS - decision["failed_count"])
        get_logger().warning(f"Dashboard login failed from {key}")
        raise HTTPException(status_code=401, detail=f"密码错误，剩余尝试次数 {remaining}")
    token = await _create_session(expected)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                        max_age=SESSION_TTL_SECONDS, secure=True, path="/")
    await asyncio.to_thread(_storage_call, "add_audit_log", "LOGIN", {"ip": key}, ip_address=key)
    return _ok({"authenticated": True})


@router.get("/auth/me")
async def auth_me(request: Request, dashboard_session: Optional[str] = Cookie(None)):
    await require_auth(request, dashboard_session)
    model = str(get_settings().get("config.model", ""))
    return _ok({"authenticated": True, "model": model,
                "version": os.environ.get("DASHBOARD_VERSION", "1.0.0")})


@router.post("/auth/logout")
async def auth_logout(request: Request, response: Response,
                      dashboard_session: Optional[str] = Cookie(None)):
    await require_auth(request, dashboard_session)
    require_same_origin(request)
    # revoke whichever credential authenticated this request: the cookie token
    # and the bearer token are both real sessions in shared storage, and a scripted
    # client logging out via the bearer path must lose access immediately
    password = await asyncio.to_thread(_admin_password)
    if not password:
        raise HTTPException(status_code=503, detail="会话吊销失败，请稍后重试")
    token_hashes = set()
    if dashboard_session and (token_hash := _session_hash(dashboard_session, password)):
        token_hashes.add(token_hash)
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer ") and (token_hash := _session_hash(auth[7:].strip(), password)):
        token_hashes.add(token_hash)

    if not token_hashes or not await asyncio.to_thread(
            _storage_call, "revoke_sessions", token_hashes):
        raise HTTPException(status_code=503, detail="会话吊销失败，请稍后重试")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return _ok(message="已退出登录")


# ------------------------------------------------------------------- config

class ConfigUpdateRequest(BaseModel):
    model_config = {"extra": "allow"}
    restart: StrictBool = False


@router.get("/config")
async def get_config(request: Request, dashboard_session: Optional[str] = Cookie(None)):
    await require_auth(request, dashboard_session)
    return _ok(await asyncio.to_thread(get_config_engine().read))


@router.put("/config")
async def put_config(body: ConfigUpdateRequest, request: Request,
                     dashboard_session: Optional[str] = Cookie(None)):
    await require_auth(request, dashboard_session)
    require_same_origin(request)
    payload = body.model_dump()
    restart = payload.pop("restart")
    fields = payload
    engine = get_config_engine()
    success, errors = await asyncio.to_thread(engine.write, fields)
    if not success:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    hot_reload_pending = any("hot reload failed" in warning for warning in errors)
    persistence_warning = next(
        (warning for warning in errors if "directory sync failed" in warning), "")
    await asyncio.to_thread(
        _storage_call, "add_audit_log", "UPDATE_CONFIG", {"fields": sorted(fields.keys())},
        ip_address=_client_ip(request))
    # report what actually happened: without docker inside the container the
    # restart never starts, and the UI must not wait for one
    result = await asyncio.to_thread(ops.restart_container) if restart else None
    restart_started = bool(restart and result and result.get("started"))
    restarted = bool(restart_started and result.get("completed") and result.get("exit_code") == 0)
    if restart:
        await asyncio.to_thread(
            _storage_call, "add_audit_log", "RESTART_CONTAINER",
            {"source": "config_save", "started": restart_started,
             "completed": bool(result and result.get("completed")),
             "exit_code": (result or {}).get("exit_code")},
            ip_address=_client_ip(request))
    if hot_reload_pending:
        message = "配置已保存，但热重载失败，需要重启"
    elif persistence_warning:
        message = "配置已保存并热生效，但崩溃持久性同步未确认"
    else:
        message = "配置已保存并热生效"
    if restarted:
        message += "，容器已完成重启"
    elif restart_started:
        message += "，重启指令已下发，完成状态待确认"
    elif restart:
        message += "，但重启未发起，请检查受控 Docker 端点或在宿主机重启"
    return _ok({"restarted": restarted,
                "restart_started": restart_started,
                "restart_output": (result or {}).get("output", []) if restart else [],
                "hot_reload_pending": hot_reload_pending,
                "reload_warning": next(
                    (warning for warning in errors if "hot reload failed" in warning), ""),
                "persistence_warning": persistence_warning},
               message=message)


# --------------------------------------------------------------------- ops

@router.get("/ops/capabilities")
async def ops_capabilities(request: Request, dashboard_session: Optional[str] = Cookie(None)):
    await require_auth(request, dashboard_session)
    return _ok({"git_pull": await asyncio.to_thread(ops.git_pull_capability)})


@router.post("/ops/restart")
async def ops_restart(request: Request, dashboard_session: Optional[str] = Cookie(None)):
    await require_auth(request, dashboard_session)
    require_same_origin(request)
    result = await asyncio.to_thread(ops.restart_container)
    started = bool(result.get("started"))
    await asyncio.to_thread(
        _storage_call, "add_audit_log", "RESTART_CONTAINER", {"started": started},
        ip_address=_client_ip(request))
    if not started:
        return JSONResponse(
            status_code=503,
            content={"success": False, "code": "OPERATION_NOT_STARTED",
                     "message": (result.get("output") or ["容器重启未发起"])[0], "data": result})
    return _ok(result, message="容器重启指令已下发")


@router.post("/ops/git-pull")
async def ops_git_pull(request: Request, dashboard_session: Optional[str] = Cookie(None)):
    await require_auth(request, dashboard_session)
    require_same_origin(request)
    result = await asyncio.to_thread(ops.git_pull)
    started = bool(result.get("started"))
    await asyncio.to_thread(
        _storage_call, "add_audit_log", "GIT_PULL",
        {"started": started, "completed": bool(result.get("completed")),
         "exit_code": result.get("exit_code"),
         "timed_out": bool(result.get("timed_out"))},
        ip_address=_client_ip(request))
    if not started:
        return JSONResponse(
            status_code=503,
            content={"success": False, "code": "OPERATION_NOT_STARTED",
                     "message": (result.get("output") or ["git pull 未发起"])[0], "data": result})
    if result.get("exit_code") != 0:
        return JSONResponse(
            status_code=500,
            content={"success": False, "code": "OPERATION_FAILED",
                     "message": (result.get("output") or ["git pull 执行失败"])[-1], "data": result})
    return _ok(result, message="git pull 已完成")


@router.post("/ops/diagnose")
async def ops_diagnose(request: Request, dashboard_session: Optional[str] = Cookie(None)):
    await require_auth(request, dashboard_session)
    require_same_origin(request)
    # probes make network calls; keep them off the shared webhook event loop
    return _ok(await asyncio.to_thread(ops.diagnose))


@router.get("/ops/logs")
async def ops_logs(request: Request, dashboard_session: Optional[str] = Cookie(None)):
    await require_auth(request, dashboard_session)
    return _ok({"lines": await asyncio.to_thread(ops.tail_logs)})


# ------------------------------------------------------ reviews and history

@router.get("/reviews")
async def list_reviews(request: Request, dashboard_session: Optional[str] = Cookie(None),
                       repo: str = "", status: str = "", verdict: str = "",
                       trigger_type: str = "", limit: int = 50, offset: int = 0):
    await require_auth(request, dashboard_session)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    data = await _dashboard_storage_read(
        "list_reviews", repo=repo, status=status, verdict=verdict,
        trigger_type=trigger_type, limit=limit, offset=offset)
    return _ok(data)


@router.get("/reviews/{review_id}")
async def review_detail(review_id: int, request: Request,
                        dashboard_session: Optional[str] = Cookie(None)):
    await require_auth(request, dashboard_session)
    detail = await _dashboard_storage_read("get_review_detail", review_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    return _ok(detail)


@router.post("/reviews/{review_id}/retry")
async def retry_review(review_id: int, request: Request,
                       dashboard_session: Optional[str] = Cookie(None)):
    await require_auth(request, dashboard_session)
    return coming_soon()  # wired to the playground runner when F-01 lights up


@router.get("/repos")
async def list_repos(request: Request, dashboard_session: Optional[str] = Cookie(None)):
    await require_auth(request, dashboard_session)
    return _ok({"items": await _dashboard_storage_read("list_repos")})


@router.get("/stats/overview")
async def stats_overview(request: Request, dashboard_session: Optional[str] = Cookie(None)):
    await require_auth(request, dashboard_session)
    return _ok(await _dashboard_storage_read("stats_overview"))


@router.get("/audit-logs")
async def audit_logs(request: Request, dashboard_session: Optional[str] = Cookie(None),
                     limit: int = 100):
    await require_auth(request, dashboard_session)
    items = await _dashboard_storage_read(
        "list_audit_logs", limit=max(1, min(limit, 500)))
    return _ok({"items": items})


# --------------------------------------------------------------- playground

class PlaygroundRunRequest(BaseModel):
    pr_url: str = Field(min_length=10, max_length=500)
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    extra_instructions: Optional[str] = None


@router.post("/playground/run")
async def playground_run(body: PlaygroundRunRequest, request: Request,
                         dashboard_session: Optional[str] = Cookie(None)):
    await require_auth(request, dashboard_session)
    # Phase 1 ships the storage + API surface; streaming execution lands with F-01.
    return coming_soon()


# ------------------------------------------- reserved routes (future phases)

_RESERVED = (
    "/commands",
    "/issues",
    "/stats/hotspots",
    "/prompt-rules",
    "/config/versions",
    "/config/rollback",
    "/alerts/channels",
    "/alerts/test",
    "/stats/cost",
    "/settings/budget",
    "/playground/compare",
    "/tools/glob-test",
    "/webhooks",
)


_RESERVED_PREFIXES = (
    "alerts/", "issues/", "webhooks/", "prompt-rules",
    "config/versions", "config/rollback", "stats/", "settings/", "tools/", "commands",
)


@router.get("/{reserved_path:path}")
@router.post("/{reserved_path:path}")
@router.put("/{reserved_path:path}")
@router.patch("/{reserved_path:path}")
async def reserved(reserved_path: str, request: Request,
                   dashboard_session: Optional[str] = Cookie(None)):
    await require_auth(request, dashboard_session)
    if f"/{reserved_path.rstrip('/')}" in _RESERVED or reserved_path.startswith(_RESERVED_PREFIXES):
        return coming_soon()
    raise HTTPException(status_code=404, detail="未知接口")
