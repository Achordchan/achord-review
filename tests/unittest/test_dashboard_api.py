"""Tests for the dashboard API routes (pr_agent/servers/dashboard_api.py)."""

import pytest
from fastapi.testclient import TestClient

import pr_agent.servers.dashboard_api as dashboard_api
from pr_agent.dashboard.storage import DashboardStorage
from pr_agent.servers.dashboard_api import router


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
    app.include_router(router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_lockout_state():
    """Tests share the process with real lockout maps; reset between tests."""
    dashboard_api._failed_attempts.clear()
    dashboard_api._sessions.clear()
    yield
    dashboard_api._failed_attempts.clear()
    dashboard_api._sessions.clear()


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


class TestClientIp:
    def test_trusted_hops_strip_client_spoofing(self, client, monkeypatch):
        from fastapi import Request

        def make_request(xff):
            headers = {"x-forwarded-for": xff} if xff else {"host": "h"}
            scope = {
                "type": "http", "method": "GET", "path": "/", "headers": [],
                "query_string": b"", "client": ("10.0.0.1", 1234), "server": ("h", 80),
                "test_headers": headers,
            }
            req = Request(scope)
            req._headers = __import__("starlette.datastructures", fromlist=["Headers"]).Headers(headers)
            return req

        # one trusted hop: last value is what our nginx saw, the one before it
        # is the real client — anything the client prepended is ignored
        assert dashboard_api._client_ip(make_request("1.2.3.4, 5.6.7.8")) == "1.2.3.4"
        # attacker rotating the FIRST value cannot change the derived key
        assert dashboard_api._client_ip(make_request("evil, 1.2.3.4, 5.6.7.8")) == "1.2.3.4"
        # no header: fall back to the socket address
        assert dashboard_api._client_ip(make_request(None)) == "10.0.0.1"
        # zero trusted hops: the raw socket address wins
        monkeypatch.setattr(dashboard_api, "TRUSTED_PROXY_HOPS", 0)
        assert dashboard_api._client_ip(make_request("1.2.3.4, 5.6.7.8")) == "10.0.0.1"


class TestProtectedRoutes:
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
