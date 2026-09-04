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
from contextlib import contextmanager
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, BackgroundTasks, Cookie, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictBool

from pr_agent.config_loader import get_settings
from pr_agent.dashboard import ops
from pr_agent.dashboard.config_engine import (
    MAX_ADMIN_PASSWORD_LENGTH,
    InvalidDashboardAdminPassword,
    get_config_engine,
    validate_admin_password,
)
from pr_agent.dashboard.env import bounded_env_int
from pr_agent.dashboard.storage import DashboardStorageReadError, get_storage
from pr_agent.dashboard.version import get_app_version
from pr_agent.log import get_logger

router = APIRouter(prefix="/api/v1/dashboard")

SESSION_COOKIE = "dashboard_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60
MAX_SQLITE_INTEGER = 2 ** 63 - 1
MAX_DASHBOARD_REQUEST_BYTES = 64 * 1024
# Everything the panel serves: the JSON API and the SPA bundle under /dashboard.
DASHBOARD_PATH_PREFIXES = ("/api/v1/dashboard", "/dashboard")
# Number of trusted proxy hops in front of this service. The deployment sits
# behind exactly one nginx; only headers appended by those hops are consumed,
# so clients cannot rotate their X-Forwarded-For to evade the login lockout.
TRUSTED_PROXY_HOPS = bounded_env_int("DASHBOARD_TRUSTED_PROXY_HOPS", 0, 0)
DASHBOARD_EXTERNAL_ORIGIN = os.environ.get("DASHBOARD_EXTERNAL_ORIGIN", "").strip()

# Session and lockout state is persisted in the dashboard SQLite database so
# login remains stable across gunicorn workers. Bearer tokens and source
# addresses enter the database only as one-way identifiers, never as credentials.
MAX_LOCKOUT_KEYS = 10_000
_password_sync_state = {
    "db_path": "", "password": None, "generation": None, "signature": None,
}
_password_sync_lock = threading.Lock()


async def add_dashboard_security_headers(request: Request, call_next):
    """Forbid framing of the dashboard and its API.

    The session cookie is SameSite=Lax and the CSRF check only compares
    origins, so a frame hosted on any sibling subdomain would issue fully
    authenticated requests: a single tricked click could restart the
    container or pull git. Nothing legitimately embeds the panel.
    """
    response = await call_next(request)
    if request.url.path.startswith(DASHBOARD_PATH_PREFIXES):
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        response.headers["X-Frame-Options"] = "DENY"
    return response


async def limit_dashboard_request_body(request: Request, call_next):
    """Reject large dashboard writes before FastAPI parses request models."""
    if (not request.url.path.startswith("/api/v1/dashboard")
            or request.method not in {"POST", "PUT", "PATCH"}):
        return await call_next(request)

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            parsed_length = int(content_length)
            if parsed_length < 0:
                raise ValueError("content length must not be negative")
            if parsed_length > MAX_DASHBOARD_REQUEST_BYTES:
                return _request_too_large()
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"success": False, "code": "INVALID_CONTENT_LENGTH",
                         "message": "请求长度无效"})

    chunks = []
    total_bytes = 0
    async for chunk in request.stream():
        total_bytes += len(chunk)
        if total_bytes > MAX_DASHBOARD_REQUEST_BYTES:
            return _request_too_large()
        chunks.append(chunk)
    request._body = b"".join(chunks)
    return await call_next(request)


def _request_too_large() -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content={"success": False, "code": "REQUEST_TOO_LARGE",
                 "message": "请求内容超过 64 KiB 限制"})


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
            return validate_admin_password(password), ("environment", variable_name)
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


def _signature_key(signature) -> str:
    return repr(signature) if signature is not None else ""


def _sync_admin_password(
        password: str, signature=None,
        allow_same_password_signature_change: bool = False) -> bool:
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
        if not storage.sync_admin_password(
                password, _signature_key(signature),
                allow_same_password_signature_change=allow_same_password_signature_change):
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
    with _admin_password_guard() as (password, signature):
        return _sync_admin_password(password, signature)


@contextmanager
def _admin_password_guard():
    """Keep controlled config writes outside password validation and synchronization."""
    if any(os.environ.get(name, "") for name in (
            "DASHBOARD_ADMIN_PASSWORD", "DASHBOARD__ADMIN_PASSWORD")):
        yield _admin_password_snapshot()
        return
    engine = get_config_engine()
    with engine.auth_read_lock():
        yield _admin_password_snapshot()


def _acknowledge_config_save(raw: Dict[str, Any], signature: tuple) -> bool:
    """Trust a same-password signature produced by ConfigEngine under its write lock."""
    for variable_name in ("DASHBOARD_ADMIN_PASSWORD", "DASHBOARD__ADMIN_PASSWORD"):
        password = os.environ.get(variable_name, "")
        if password:
            return _sync_admin_password(
                validate_admin_password(password), ("environment", variable_name),
                allow_same_password_signature_change=True)
    password = validate_admin_password(
        str(raw.get("dashboard", {}).get("admin_password", "") or ""))
    return _sync_admin_password(
        password, signature, allow_same_password_signature_change=True)


async def _create_session(verified_password: str) -> str:
    token = secrets.token_urlsafe(32)

    def _create() -> str:
        with _admin_password_guard() as (current_password, current_signature):
            if not hmac.compare_digest(
                    verified_password.encode("utf-8"), current_password.encode("utf-8")):
                return "password_changed"
            token_hash = _session_hash(token, verified_password)
            created = get_storage().create_session_for_password(
                token_hash,
                int(time.time()) + SESSION_TTL_SECONDS,
                current_password,
                _signature_key(current_signature))
            after_password, _ = _admin_password_snapshot()
            if not hmac.compare_digest(
                    current_password.encode("utf-8"), after_password.encode("utf-8")):
                get_storage().revoke_session(token_hash)
                return "password_changed"
            return "created" if created else "storage_error"

    try:
        result = await asyncio.to_thread(_create)
    except InvalidDashboardAdminPassword as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    if result == "password_changed":
        raise HTTPException(status_code=401, detail="管理员密码已变更，请使用新密码重新登录")
    if result != "created":
        raise HTTPException(status_code=503, detail="会话存储暂不可用，请稍后重试")
    return token


def _session_valid(token: Optional[str]) -> bool:
    with _admin_password_guard() as (password, signature):
        if not _sync_admin_password(password, signature):
            raise DashboardStorageReadError("dashboard authentication state is unavailable")
        if not token or not password:
            return False
        token_hash = _session_hash(token, password)
        valid = bool(token_hash and get_storage().session_is_valid(token_hash))
        after_password, after_signature = _admin_password_snapshot()
        if not hmac.compare_digest(password.encode("utf-8"), after_password.encode("utf-8")):
            _sync_admin_password(after_password, after_signature)
            return False
        if signature != after_signature:
            if not _sync_admin_password(after_password, after_signature):
                raise DashboardStorageReadError("dashboard authentication state is unavailable")
            return False
        return valid


async def require_auth(request: Request, dashboard_session: Optional[str] = Cookie(None)) -> None:
    try:
        if not await asyncio.to_thread(_session_valid, dashboard_session):
            # also accept a bearer token for scripted access
            auth = request.headers.get("authorization", "")
            if auth.startswith("Bearer ") and await asyncio.to_thread(_session_valid, auth[7:].strip()):
                return
            raise HTTPException(status_code=401, detail="Not authenticated")
    except (DashboardStorageReadError, InvalidDashboardAdminPassword) as e:
        get_logger().warning(f"Dashboard session validation unavailable, error: {e}")
        raise HTTPException(status_code=503, detail="会话校验暂不可用，请检查配置后重试") from e


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
    password: str = Field(min_length=1, max_length=MAX_ADMIN_PASSWORD_LENGTH)


@router.post("/auth/login")
async def auth_login(body: LoginRequest, request: Request, response: Response):
    key = _lockout_key(request)
    try:
        expected, _ = await asyncio.to_thread(_admin_password_snapshot)
    except InvalidDashboardAdminPassword as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    if not expected:
        # Recording the disabled state still touches storage, so an outage here
        # must read as the same retryable 503 the data routes return, not a 500.
        try:
            await asyncio.to_thread(_sync_current_admin_password)
        except DashboardStorageReadError as e:
            get_logger().warning(f"Dashboard auth state sync unavailable, error: {e}")
            raise HTTPException(status_code=503, detail="会话存储暂不可用，请稍后重试") from e
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
                "version": get_app_version()})


@router.post("/auth/logout")
async def auth_logout(request: Request, response: Response,
                      dashboard_session: Optional[str] = Cookie(None)):
    await require_auth(request, dashboard_session)
    require_same_origin(request)
    # revoke whichever credential authenticated this request: the cookie token
    # and the bearer token are both real sessions in shared storage, and a scripted
    # client logging out via the bearer path must lose access immediately
    try:
        password = await asyncio.to_thread(_admin_password)
    except InvalidDashboardAdminPassword as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
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
                     background_tasks: BackgroundTasks,
                     dashboard_session: Optional[str] = Cookie(None)):
    await require_auth(request, dashboard_session)
    require_same_origin(request)
    payload = body.model_dump()
    restart = payload.pop("restart")
    fields = payload
    engine = get_config_engine()
    success, errors = await asyncio.to_thread(engine.write, fields, _acknowledge_config_save)
    if not success:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    hot_reload_pending = any("hot reload failed" in warning for warning in errors)
    persistence_warning = next(
        (warning for warning in errors if "directory sync failed" in warning), "")
    auth_sync_warning = next(
        (warning for warning in errors if "auth state synchronization failed" in warning), "")
    await asyncio.to_thread(
        _storage_call, "add_audit_log", "UPDATE_CONFIG", {"fields": sorted(fields.keys())},
        ip_address=_client_ip(request))
    # report what actually happened: without docker inside the container the
    # restart never starts, and the UI must not wait for one
    result, restart_ticket = await asyncio.to_thread(ops.prepare_restart) if restart else (None, None)
    restart_started = bool(restart and result and result.get("started"))
    restarted = bool(restart_started and result.get("completed") and result.get("exit_code") == 0)
    if restart:
        await asyncio.to_thread(
            _storage_call, "add_audit_log", "RESTART_CONTAINER",
            {"source": "config_save", "started": restart_started,
             "scheduled": restart_ticket is not None,
             "completed": bool(result and result.get("completed")),
             "exit_code": (result or {}).get("exit_code")},
            ip_address=_client_ip(request))
        if restart_ticket is not None:
            background_tasks.add_task(ops.execute_restart, restart_ticket)
    if auth_sync_warning:
        message = "配置已保存，但认证状态同步失败，现有会话将被安全吊销"
    elif hot_reload_pending:
        message = "配置已保存，但热重载失败，需要重启"
    elif persistence_warning:
        message = "配置已保存并热生效，但崩溃持久性同步未确认"
    else:
        message = "配置已保存并热生效"
    if restarted:
        message += "，容器已完成重启"
    elif restart_started:
        message += "，重启已排队，将在当前响应后执行"
    elif restart:
        message += "，但重启未发起，请检查受控 Docker 端点或在宿主机重启"
    return _ok({"restarted": restarted,
                "restart_started": restart_started,
                "restart_output": (result or {}).get("output", []) if restart else [],
                "hot_reload_pending": hot_reload_pending,
                "auth_sync_warning": auth_sync_warning,
                "reload_warning": next(
                    (warning for warning in errors if "hot reload failed" in warning), ""),
                "persistence_warning": persistence_warning},
               message=message)


def _upstream_model_urls(api_base: str) -> list:
    """Candidate model-listing endpoints for an OpenAI-compatible relay.

    Bases are written either with the version segment (…/v1) or without it, and
    relays expose the catalog as /models or, occasionally, /model — so try the
    plausible combinations in order rather than guessing one.
    """
    base = api_base.strip().rstrip("/")
    if base.endswith("/v1"):
        return [f"{base}/models", f"{base}/model"]
    return [f"{base}/v1/models", f"{base}/v1/model", f"{base}/models", f"{base}/model"]


def _parse_model_ids(payload: Any) -> list:
    """Extract model ids from an OpenAI-style (or list/`models`) response, capped."""
    if isinstance(payload, dict):
        items = payload.get("data") or payload.get("models") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    ids = []
    for item in items:
        if isinstance(item, dict):
            value = item.get("id") or item.get("name")
        elif isinstance(item, str):
            value = item
        else:
            value = None
        if isinstance(value, str) and value.strip():
            ids.append(value.strip())
    # De-duplicate, keep stable order, and cap so a huge catalog can't flood the UI.
    seen = set()
    unique = [m for m in ids if not (m in seen or seen.add(m))]
    return sorted(unique)[:200]


@router.get("/config/upstream-models")
async def get_upstream_models(request: Request, dashboard_session: Optional[str] = Cookie(None)):
    """List models the configured relay advertises, so the operator can pick one
    instead of typing it. Uses the live api_base/key, never the masked GET value."""
    await require_auth(request, dashboard_session)
    api_base = str(get_settings().get("openai.api_base", "") or "").strip()
    api_key = str(get_settings().get("openai.key", "") or "").strip()
    if not api_base:
        raise HTTPException(status_code=400, detail="未配置中继 API Base，无法获取上游模型")
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    last_error = "上游未返回可用模型列表"
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            for url in _upstream_model_urls(api_base):
                try:
                    resp = await client.get(url, headers=headers)
                except httpx.HTTPError as e:
                    last_error = f"请求失败：{e}"
                    continue
                if resp.status_code != 200:
                    last_error = f"{url} 返回 HTTP {resp.status_code}"
                    continue
                try:
                    models = _parse_model_ids(resp.json())
                except ValueError:
                    last_error = f"{url} 返回的不是有效 JSON"
                    continue
                if models:
                    return _ok({"models": models})
                last_error = f"{url} 未返回模型列表"
    except Exception as e:  # never leak an internal trace to the panel
        raise HTTPException(status_code=502, detail=f"获取上游模型失败：{e}")
    raise HTTPException(status_code=502, detail=last_error[:300])


# --------------------------------------------------------------------- ops

@router.get("/ops/capabilities")
async def ops_capabilities(request: Request, dashboard_session: Optional[str] = Cookie(None)):
    await require_auth(request, dashboard_session)
    git_pull, restart, rebuild_required = await asyncio.gather(
        asyncio.to_thread(ops.git_pull_capability),
        asyncio.to_thread(ops.restart_capability),
        asyncio.to_thread(ops.rebuild_required),
    )
    # rebuild_required is server-authoritative and stateless, so the panel can
    # restore the "needs a host rebuild, restart blocked" state after a reload.
    return _ok({"git_pull": git_pull, "restart": restart,
                "rebuild_required": rebuild_required})


@router.post("/ops/restart")
async def ops_restart(request: Request, background_tasks: BackgroundTasks,
                      dashboard_session: Optional[str] = Cookie(None)):
    await require_auth(request, dashboard_session)
    require_same_origin(request)
    result, restart_ticket = await asyncio.to_thread(ops.prepare_restart)
    started = bool(result.get("started"))
    await asyncio.to_thread(
        _storage_call, "add_audit_log", "RESTART_CONTAINER",
        {"started": started, "scheduled": restart_ticket is not None},
        ip_address=_client_ip(request))
    if not started:
        return JSONResponse(
            status_code=503,
            content={"success": False, "code": "OPERATION_NOT_STARTED",
                     "message": (result.get("output") or ["容器重启未发起"])[0], "data": result})
    if restart_ticket is not None:
        background_tasks.add_task(ops.execute_restart, restart_ticket)
    return _ok(result, message="容器重启已排队")


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


@router.get("/ops/check-update")
async def ops_check_update(request: Request, dashboard_session: Optional[str] = Cookie(None)):
    await require_auth(request, dashboard_session)
    # git fetch reaches the network; keep it off the shared webhook event loop
    return _ok(await asyncio.to_thread(ops.check_update))


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
    offset = max(0, min(offset, MAX_SQLITE_INTEGER))
    data = await _dashboard_storage_read(
        "list_reviews", repo=repo, status=status, verdict=verdict,
        trigger_type=trigger_type, limit=limit, offset=offset)
    return _ok(data)


@router.get("/reviews/{review_id}")
async def review_detail(review_id: int, request: Request,
                        dashboard_session: Optional[str] = Cookie(None)):
    await require_auth(request, dashboard_session)
    if not 1 <= review_id <= MAX_SQLITE_INTEGER:
        raise HTTPException(status_code=404, detail="Review not found")
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
