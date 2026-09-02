"""Tests for the dashboard API routes (pr_agent/servers/dashboard_api.py)."""

import pytest
from fastapi.testclient import TestClient

import pr_agent.servers.dashboard_api as dashboard_api
from pr_agent.dashboard.storage import DashboardStorage


@pytest.fixture()
def storage(tmp_path, monkeypatch):
    store = DashboardStorage(db_path=str(tmp_path / "api_test.db"))
    store.initialize()
    monkeypatch.setattr(dashboard_api, "get_storage", lambda: store)
    monkeypatch.setenv("DASHBOARD_ADMIN_PASSWORD", "test-pass-123")
    return store


@pytest.fixture()
def client(storage, monkeypatch):
    # avoid touching the real config engine during API tests
    class StubEngine:
        def read(self):
            return {"available": False, "path": None, "values": {}}

        def write(self, fields):
            return True, []

    monkeypatch.setattr(dashboard_api, "get_config_engine", lambda: StubEngine())
    # FastAPI app with only the dashboard router
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(dashboard_api.router)
    return TestClient(app)


def _login(client):
    resp = client.post("/api/v1/dashboard/auth/login", json={"password": "test-pass-123"})
    assert resp.status_code == 200, resp.text
    token = resp.cookies.get(dashboard_api.SESSION_COOKIE)
    return {"cookies": {dashboard_api.SESSION_COOKIE: token}}


def _auth_header(client):
    token = _login(client)["cookies"][dashboard_api.SESSION_COOKIE]
    # TestClient speaks plain http, so the Secure cookie is not echoed back;
    # every test authenticates through the equivalent bearer path instead.
    return {"Authorization": f"Bearer {token}"}


class TestAuth:
    def test_login_success_and_me(self, client):
        auth = _auth_header(client)
        resp = client.get("/api/v1/dashboard/auth/me", headers=auth)
        assert resp.status_code == 200
        assert resp.json()["data"]["authenticated"] is True

    def test_login_wrong_password(self, client):
        resp = client.post("/api/v1/dashboard/auth/login", json={"password": "wrong"})
        assert resp.status_code == 401

    def test_routes_require_auth(self, client):
        for path in ["/api/v1/dashboard/config", "/api/v1/dashboard/reviews",
                     "/api/v1/dashboard/stats/overview", "/api/v1/dashboard/audit-logs"]:
            assert client.get(path).status_code == 401, path

    def test_lockout_after_five_failures(self, client, monkeypatch):
        for _ in range(5):
            client.post("/api/v1/dashboard/auth/login", json={"password": "bad"})
        resp = client.post("/api/v1/dashboard/auth/login", json={"password": "test-pass-123"})
        assert resp.status_code == 429

    def test_no_password_configured(self, client, monkeypatch):
        monkeypatch.delenv("DASHBOARD_ADMIN_PASSWORD", raising=False)
        monkeypatch.setattr(dashboard_api, "_admin_password", lambda: "")
        resp = client.post("/api/v1/dashboard/auth/login", json={"password": "x"})
        assert resp.status_code == 503

    def test_logout_revokes_bearer_session(self, client):
        token = _auth_header(client)["Authorization"][7:]
        resp = client.post("/api/v1/dashboard/auth/logout",
                           headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        # the bearer session must be dead immediately, not after cookie expiry
        assert not dashboard_api._session_valid(token)
        assert client.get("/api/v1/dashboard/auth/me",
                          headers={"Authorization": f"Bearer {token}"}).status_code == 401


class TestClientIp:
    def test_trusted_hops_read_rightmost_entries(self, client, monkeypatch):
        from fastapi import Request
        from starlette.datastructures import Headers

        def make_request(xff):
            headers = {"x-forwarded-for": xff} if xff else {"host": "h"}
            scope = {
                "type": "http", "method": "GET", "path": "/", "headers": [],
                "query_string": b"", "client": ("10.0.0.1", 1234), "server": ("h", 80),
            }
            req = Request(scope)
            req._headers = Headers(headers)
            return req

        # one trusted hop: nginx APPENDS the address it saw, so the rightmost
        # entry is the real client; the leftmost is client-supplied noise
        assert dashboard_api._client_ip(make_request("1.2.3.4, 5.6.7.8")) == "5.6.7.8"
        # rotating the prepended values cannot change the derived lockout key
        assert dashboard_api._client_ip(make_request("evil, 1.2.3.4, 5.6.7.8")) == "5.6.7.8"
        # no header: fall back to the socket address
        assert dashboard_api._client_ip(make_request(None)) == "10.0.0.1"
        # two trusted hops, chain shorter than hops: the header carries no
        # fully-trusted entry, fall back to the socket address
        monkeypatch.setattr(dashboard_api, "TRUSTED_PROXY_HOPS", 2)
        assert dashboard_api._client_ip(make_request("1.2.3.4")) == "10.0.0.1"
        # two hops: second-from-right is what the outer trusted hop observed
        assert dashboard_api._client_ip(make_request("noise, 9.9.9.9, 8.8.8.8")) == "9.9.9.9"
        # zero trusted hops: the header is ignored entirely
        monkeypatch.setattr(dashboard_api, "TRUSTED_PROXY_HOPS", 0)
        assert dashboard_api._client_ip(make_request("1.2.3.4, 5.6.7.8")) == "10.0.0.1"

    def test_lockout_map_bounded(self, client, storage, monkeypatch):
        from fastapi import Request
        from starlette.datastructures import Headers

        def make_request(ip):
            scope = {
                "type": "http", "method": "GET", "path": "/", "headers": [],
                "query_string": b"", "client": (ip, 1234), "server": ("h", 80),
            }
            req = Request(scope)
            req._headers = Headers({"host": "h"})
            return req

        monkeypatch.setattr(dashboard_api, "TRUSTED_PROXY_HOPS", 0)
        monkeypatch.setattr(dashboard_api, "MAX_LOCKOUT_KEYS", 10)
        for i in range(200):
            dashboard_api._record_failed_attempt(dashboard_api._lockout_key(make_request(f"10.0.{i}.1")))
        assert storage.login_attempt_row_count() <= 10 * dashboard_api.MAX_FAILED_ATTEMPTS
        newest = dashboard_api._credential_hash("10.0.199.1")
        assert storage.failed_login_count(newest, 0) == 1

    def test_lockout_entries_expire(self, client, storage, monkeypatch):
        import time as _time

        from fastapi import Request
        from starlette.datastructures import Headers

        scope = {
            "type": "http", "method": "GET", "path": "/", "headers": [],
            "query_string": b"", "client": ("10.9.9.9", 1234), "server": ("h", 80),
        }
        req = Request(scope)
        req._headers = Headers({"host": "h"})
        monkeypatch.setattr(dashboard_api, "TRUSTED_PROXY_HOPS", 0)
        key = dashboard_api._lockout_key(req)
        key_hash = dashboard_api._credential_hash(key)
        storage.record_failed_login(
            key_hash, _time.time() - dashboard_api.LOCKOUT_SECONDS - 1,
            dashboard_api.LOCKOUT_SECONDS, 100)
        # the next failed login removes attempts older than the lockout window
        dashboard_api._record_failed_attempt("some-other-key")
        assert storage.failed_login_count(key_hash, 0) == 0


class TestSameOrigin:
    def _request(self, headers):
        from fastapi import Request
        from starlette.datastructures import Headers as SHeaders

        scope = {
            "type": "http", "method": "POST", "path": "/", "headers": [],
            "query_string": b"", "client": ("10.0.0.1", 1234), "server": ("h", 80),
        }
        req = Request(scope)
        req._headers = SHeaders(headers)
        return req

    def test_fetch_metadata_same_origin_passes(self, client):
        dashboard_api.require_same_origin(self._request({"sec-fetch-site": "same-origin"}))

    def test_fetch_metadata_cross_site_rejected(self, client):
        import pytest as _pytest
        from fastapi import HTTPException

        with _pytest.raises(HTTPException) as exc:
            dashboard_api.require_same_origin(self._request({"sec-fetch-site": "cross-site"}))
        assert exc.value.status_code == 403

    def test_origin_match_passes(self, client):
        dashboard_api.require_same_origin(self._request({
            "origin": "https://review.achord.cn", "host": "review.achord.cn"}))

    def test_origin_mismatch_rejected(self, client):
        import pytest as _pytest
        from fastapi import HTTPException

        with _pytest.raises(HTTPException) as exc:
            dashboard_api.require_same_origin(self._request({
                "origin": "https://evil.example", "host": "review.achord.cn"}))
        assert exc.value.status_code == 403

    def test_no_evidence_rejected(self, client):
        import pytest as _pytest
        from fastapi import HTTPException

        with _pytest.raises(HTTPException) as exc:
            dashboard_api.require_same_origin(self._request({}))
        assert exc.value.status_code == 403

    def test_mutations_require_origin_evidence(self, client):
        # a bodyless cookie-authenticated POST without any origin header is now 403
        auth = _auth_header(client)
        resp = client.post("/api/v1/dashboard/ops/git-pull", headers=auth)
        assert resp.status_code == 403


class TestProtectedRoutes:
    def test_config_save_without_restart_reports_not_restarted(self, client):
        auth = _auth_header(client)
        resp = client.put(
            "/api/v1/dashboard/config",
            headers={**auth, "Sec-Fetch-Site": "same-origin"},
            json={"model": "openai/gpt-test", "restart": False})
        assert resp.status_code == 200
        assert resp.json()["data"]["restarted"] is False
        assert resp.json()["data"]["restart_started"] is False

    def test_config_rejects_string_restart_flag(self, client):
        auth = _auth_header(client)
        resp = client.put(
            "/api/v1/dashboard/config",
            headers={**auth, "Sec-Fetch-Site": "same-origin"},
            json={"model": "openai/gpt-test", "restart": "false"})
        assert resp.status_code == 422

    def test_ops_reports_command_not_started(self, client, monkeypatch):
        auth = _auth_header(client)
        monkeypatch.setattr(
            dashboard_api.ops, "restart_container",
            lambda: {"started": False, "task_id": None, "already_running": False,
                     "output": ["docker unavailable"]})
        resp = client.post(
            "/api/v1/dashboard/ops/restart",
            headers={**auth, "Sec-Fetch-Site": "same-origin"})
        assert resp.status_code == 503
        assert resp.json()["code"] == "OPERATION_NOT_STARTED"
        assert resp.json()["data"]["task_id"] is None

    def test_audit_log_limit_is_clamped_at_lower_bound(self, client, storage, monkeypatch):
        captured = {}
        original = storage.list_audit_logs

        def capture_limit(limit):
            captured["limit"] = limit
            return original(limit)

        monkeypatch.setattr(storage, "list_audit_logs", capture_limit)
        auth = _auth_header(client)
        resp = client.get("/api/v1/dashboard/audit-logs?limit=-1", headers=auth)
        assert resp.status_code == 200
        assert captured["limit"] == 1

    def test_reviews_list_and_detail(self, client, storage):
        request_id = storage.create_review(repo_name="a/b", pr_number=1, pr_url="u")
        storage.complete_review(request_id, verdict="APPROVE")
        review_id = storage.get_review_by_request_id(request_id)["id"]
        auth = _auth_header(client)

        resp = client.get("/api/v1/dashboard/reviews", headers=auth)
        assert resp.status_code == 200
        assert resp.json()["data"]["total"] == 1

        resp = client.get(f"/api/v1/dashboard/reviews/{review_id}", headers=auth)
        assert resp.status_code == 200
        assert resp.json()["data"]["verdict"] == "APPROVE"

        resp = client.get("/api/v1/dashboard/reviews/99999", headers=auth)
        assert resp.status_code == 404

    def test_stats_overview(self, client, storage):
        request_id = storage.create_review(repo_name="a/b", pr_number=1, pr_url="u")
        storage.complete_review(request_id)
        auth = _auth_header(client)
        resp = client.get("/api/v1/dashboard/stats/overview", headers=auth)
        assert resp.status_code == 200
        assert resp.json()["data"]["total"] == 1

    def test_reserved_routes_come_soon(self, client):
        auth = _auth_header(client)
        for path in ["/api/v1/dashboard/commands", "/api/v1/dashboard/issues",
                     "/api/v1/dashboard/stats/hotspots", "/api/v1/dashboard/alerts/channels",
                     "/api/v1/dashboard/config/versions", "/api/v1/dashboard/webhooks"]:
            resp = client.get(path, headers=auth)
            assert resp.status_code == 501, path
            assert resp.json()["code"] == "COMING_SOON"

    def test_unknown_route_404(self, client):
        auth = _auth_header(client)
        assert client.get("/api/v1/dashboard/nothing/here", headers=auth).status_code == 404
