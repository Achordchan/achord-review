"""Dashboard API: admin auth, live config, one-click ops, review history and playground.

Mounted by pr_agent/servers/github_app.py under /api/v1/dashboard. All routes
require the admin session except /auth/login; the storage layer is fail-safe
(failures degrade to logs, never to webhook errors).

Routes reserved for future phases return 501 with code COMING_SOON so the
front end can wire them up before the backing features ship.
"""

import asyncio
import hmac
import os
import secrets
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from pr_agent.config_loader import get_settings
from pr_agent.dashboard import ops
from pr_agent.dashboard.config_engine import get_config_engine
from pr_agent.dashboard.storage import get_storage
from pr_agent.log import get_logger

router = APIRouter(prefix="/api/v1/dashboard")

SESSION_COOKIE = "dashboard_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60
# Number of trusted proxy hops in front of this service. The deployment sits
# behind exactly one nginx; only headers appended by those hops are consumed,
# so clients cannot rotate their X-Forwarded-For to evade the login lockout.
TRUSTED_PROXY_HOPS = int(os.environ.get("DASHBOARD_TRUSTED_PROXY_HOPS", "1"))

# in-process session / lockout state; a restart simply logs everyone back in.
# Sessions live per worker process, so this deployment must run exactly one
# gunicorn worker (the shipped compose file sets GUNICORN_WORKERS=1): with
# several workers, a login against one would 401 on a sibling.
_sessions: Dict[str, float] = {}
_failed_attempts: Dict[str, list] = {}
# Global bound on tracked lockout keys. Unauthenticated requests can mint
# arbitrarily many distinct keys (per-IP, and the socket address itself is
# spoofable only up to the number of real source addresses); without a cap the
# map grows without bound. Beyond the cap the oldest entries are evicted —
# a lockout being lifted slightly early is acceptable, unbounded memory is not.
MAX_LOCKOUT_KEYS = 10_000


def _admin_password() -> str:
    password = os.environ.get("DASHBOARD_ADMIN_PASSWORD", "")
    if password:
        return password
    return str(get_settings().get("dashboard.admin_password", "") or "")


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


def _is_locked_out(key: str) -> bool:
    attempts = _failed_attempts.get(key, [])
    now = time.monotonic()
    return bool(attempts) and len(attempts) >= MAX_FAILED_ATTEMPTS and now - attempts[0] < LOCKOUT_SECONDS


def _record_failed_attempt(key: str) -> None:
    now = time.monotonic()
    attempts = [t for t in _failed_attempts.get(key, []) if now - t < LOCKOUT_SECONDS]
    attempts.append(now)
    _failed_attempts[key] = attempts
    _evict_lockout_state(now)


def _evict_lockout_state(now: float) -> None:
    """Drop expired entries and, if still over the global bound, the oldest.

    Called on each failed login: a failed login is the only event that grows
    the map, so this keeps the map proportional to actual attack pressure.
    """
    expired = [k for k, stamps in _failed_attempts.items()
               if not stamps or now - stamps[-1] >= LOCKOUT_SECONDS]
    for k in expired:
        _failed_attempts.pop(k, None)
    while len(_failed_attempts) > MAX_LOCKOUT_KEYS:
        oldest = min(_failed_attempts, key=lambda k: _failed_attempts[k][-1])
        _failed_attempts.pop(oldest, None)


def _clear_failed_attempts(key: str) -> None:
    _failed_attempts.pop(key, None)


def _create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.monotonic() + SESSION_TTL_SECONDS
    # opportunistic cleanup of expired sessions
    for stale in [t for t, exp in _sessions.items() if exp < time.monotonic()]:
        _sessions.pop(stale, None)
    return token


def _session_valid(token: Optional[str]) -> bool:
    if not token:
        return False
    expiry = _sessions.get(token)
    if expiry is None:
        return False
    if expiry < time.monotonic():
        _sessions.pop(token, None)
        return False
    return True


def require_auth(request: Request, dashboard_session: Optional[str] = Cookie(None)) -> None:
    if not _session_valid(dashboard_session):
        # also accept a bearer token for scripted access
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer ") and _session_valid(auth[7:].strip()):
            return
        raise HTTPException(status_code=401, detail="Not authenticated")


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
    if _is_locked_out(key):
        raise HTTPException(status_code=429, detail="尝试次数过多，请 15 分钟后再试")
    expected = _admin_password()
    if not expected:
        raise HTTPException(status_code=503, detail="管理员密码未配置（config.toml [dashboard] admin_password）")
    if not hmac.compare_digest(body.password, expected):
        _record_failed_attempt(key)
        remaining = max(0, MAX_FAILED_ATTEMPTS - len(_failed_attempts.get(key, [])))
        get_logger().warning(f"Dashboard login failed from {key}")
        raise HTTPException(status_code=401, detail=f"密码错误，剩余尝试次数 {remaining}")
    _clear_failed_attempts(key)
    token = _create_session()
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                        max_age=SESSION_TTL_SECONDS, secure=True, path="/")
    get_storage().add_audit_log("LOGIN", {"ip": key}, ip_address=key)
    return _ok({"authenticated": True})


@router.get("/auth/me")
async def auth_me(request: Request, dashboard_session: Optional[str] = Cookie(None)):
    require_auth(request, dashboard_session)
    model = str(get_settings().get("config.model", ""))
    return _ok({"authenticated": True, "model": model,
                "version": os.environ.get("DASHBOARD_VERSION", "1.0.0")})


@router.post("/auth/logout")
async def auth_logout(request: Request, response: Response,
                      dashboard_session: Optional[str] = Cookie(None)):
    if dashboard_session:
        _sessions.pop(dashboard_session, None)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return _ok(message="已退出登录")


# ------------------------------------------------------------------- config

class ConfigUpdateRequest(BaseModel):
    model_config = {"extra": "allow"}
    restart: bool = False


@router.get("/config")
async def get_config(request: Request, dashboard_session: Optional[str] = Cookie(None)):
    require_auth(request, dashboard_session)
    return _ok(get_config_engine().read())


@router.put("/config")
async def put_config(request: Request, dashboard_session: Optional[str] = Cookie(None)):
    require_auth(request, dashboard_session)
    body = await request.json()
    restart = bool(body.pop("restart", False))
    fields = {k: v for k, v in body.items()
              if k in ("model", "reasoning_effort", "ai_timeout", "max_model_tokens",
                       "api_base", "key", "verdict_blocking_severities",
                       "num_max_findings", "ignore_glob", "extra_instructions")}
    engine = get_config_engine()
    success, errors = engine.write(fields)
    if not success:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    get_storage().add_audit_log("UPDATE_CONFIG", {"fields": sorted(fields.keys())},
                                ip_address=_client_ip(request))
    restarted = False
    if restart:
        ops.restart_container()
        restarted = True
    return _ok({"restarted": restarted}, message="配置已保存并热生效" + ("，容器重启中" if restarted else ""))


# --------------------------------------------------------------------- ops

@router.post("/ops/restart")
async def ops_restart(request: Request, dashboard_session: Optional[str] = Cookie(None)):
    require_auth(request, dashboard_session)
    result = ops.restart_container()
    get_storage().add_audit_log("RESTART_CONTAINER", {}, ip_address=_client_ip(request))
    return _ok(result, message="容器重启指令已下发")


@router.post("/ops/git-pull")
async def ops_git_pull(request: Request, dashboard_session: Optional[str] = Cookie(None)):
    require_auth(request, dashboard_session)
    result = ops.git_pull()
    get_storage().add_audit_log("GIT_PULL", {}, ip_address=_client_ip(request))
    return _ok(result, message="git pull 已执行")


@router.get("/ops/task/{task_id}")
async def ops_task(task_id: str, request: Request, dashboard_session: Optional[str] = Cookie(None)):
    require_auth(request, dashboard_session)
    return _ok(ops.poll_task(task_id))


@router.post("/ops/diagnose")
async def ops_diagnose(request: Request, dashboard_session: Optional[str] = Cookie(None)):
    require_auth(request, dashboard_session)
    # probes make network calls; keep them off the shared webhook event loop
    return _ok(await asyncio.to_thread(ops.diagnose))


@router.get("/ops/logs")
async def ops_logs(request: Request, dashboard_session: Optional[str] = Cookie(None)):
    require_auth(request, dashboard_session)
    return _ok({"lines": ops.tail_logs()})


# ------------------------------------------------------ reviews and history

@router.get("/reviews")
async def list_reviews(request: Request, dashboard_session: Optional[str] = Cookie(None),
                       repo: str = "", status: str = "", verdict: str = "",
                       trigger_type: str = "", limit: int = 50, offset: int = 0):
    require_auth(request, dashboard_session)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    return _ok(get_storage().list_reviews(repo=repo, status=status, verdict=verdict,
                                          trigger_type=trigger_type, limit=limit, offset=offset))


@router.get("/reviews/{review_id}")
async def review_detail(review_id: int, request: Request,
                        dashboard_session: Optional[str] = Cookie(None)):
    require_auth(request, dashboard_session)
    detail = get_storage().get_review_detail(review_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    return _ok(detail)


@router.post("/reviews/{review_id}/retry")
async def retry_review(review_id: int, request: Request,
                       dashboard_session: Optional[str] = Cookie(None)):
    require_auth(request, dashboard_session)
    return coming_soon()  # wired to the playground runner when F-01 lights up


@router.get("/repos")
async def list_repos(request: Request, dashboard_session: Optional[str] = Cookie(None)):
    require_auth(request, dashboard_session)
    return _ok({"items": get_storage().list_repos()})


@router.get("/stats/overview")
async def stats_overview(request: Request, dashboard_session: Optional[str] = Cookie(None)):
    require_auth(request, dashboard_session)
    return _ok(get_storage().stats_overview())


@router.get("/audit-logs")
async def audit_logs(request: Request, dashboard_session: Optional[str] = Cookie(None),
                     limit: int = 100):
    require_auth(request, dashboard_session)
    return _ok({"items": get_storage().list_audit_logs(limit=min(limit, 500))})


# --------------------------------------------------------------- playground

class PlaygroundRunRequest(BaseModel):
    pr_url: str = Field(min_length=10, max_length=500)
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    extra_instructions: Optional[str] = None


@router.post("/playground/run")
async def playground_run(body: PlaygroundRunRequest, request: Request,
                         dashboard_session: Optional[str] = Cookie(None)):
    require_auth(request, dashboard_session)
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
    if f"/{reserved_path.rstrip('/')}" in _RESERVED or reserved_path.startswith(_RESERVED_PREFIXES):
        return coming_soon()
    raise HTTPException(status_code=404, detail="未知接口")
