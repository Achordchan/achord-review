"""Tests for the dashboard API routes (pr_agent/servers/dashboard_api.py)."""

import pytest
from fastapi.testclient import TestClient

import pr_agent.servers.dashboard_api as dashboard_api
from pr_agent.dashboard.storage import DashboardStorage, DashboardStorageReadError


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
            from pr_agent.dashboard.config_engine import _validate
            _, errors = _validate(fields)
            return not errors, errors

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
    def test_environment_password_snapshot_identifies_source_without_derived_hash(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_ADMIN_PASSWORD", "first-password")
        monkeypatch.setenv("DASHBOARD__ADMIN_PASSWORD", "fallback-password")

        password, signature = dashboard_api._admin_password_snapshot()
        _, repeated_signature = dashboard_api._admin_password_snapshot()

        assert password == "first-password"
        assert signature == repeated_signature
        assert signature == ("environment", "DASHBOARD_ADMIN_PASSWORD")
        assert password not in signature

        monkeypatch.delenv("DASHBOARD_ADMIN_PASSWORD")
        fallback_password, fallback_signature = dashboard_api._admin_password_snapshot()
        assert fallback_password == "fallback-password"
        assert fallback_signature == ("environment", "DASHBOARD__ADMIN_PASSWORD")

    def test_login_success_and_me(self, client):
        auth = _auth_header(client)
        resp = client.get("/api/v1/dashboard/auth/me", headers=auth)
        assert resp.status_code == 200
        assert resp.json()["data"]["authenticated"] is True

    def test_session_storage_failure_returns_retryable_503(self, client, storage, monkeypatch):
        auth = _auth_header(client)
        monkeypatch.setattr(
            storage, "admin_password_generation",
            lambda: (_ for _ in ()).throw(DashboardStorageReadError("volume unavailable")))

        resp = client.get("/api/v1/dashboard/auth/me", headers=auth)

        assert resp.status_code == 503
        assert "暂不可用" in resp.json()["detail"]

    def test_login_wrong_password(self, client):
        resp = client.post("/api/v1/dashboard/auth/login", json={"password": "wrong"})
        assert resp.status_code == 401

    def test_login_accepts_unicode_password(self, client, monkeypatch):
        monkeypatch.setenv("DASHBOARD_ADMIN_PASSWORD", "管理员-安全口令")
        resp = client.post(
            "/api/v1/dashboard/auth/login", json={"password": "管理员-安全口令"})
        assert resp.status_code == 200

    def test_login_rejects_password_rotated_during_request(self, client, storage, monkeypatch):
        password = {"value": "old-password"}
        monkeypatch.setattr(
            dashboard_api, "_admin_password_snapshot",
            lambda: (password["value"], (password["value"],)))
        original_verify = storage.verify_login_attempt

        def rotate_after_verification(*args, **kwargs):
            decision = original_verify(*args, **kwargs)
            password["value"] = "new-password"
            return decision

        monkeypatch.setattr(storage, "verify_login_attempt", rotate_after_verification)
        resp = client.post(
            "/api/v1/dashboard/auth/login", json={"password": "old-password"})
        assert resp.status_code == 401
        assert "已变更" in resp.json()["detail"]

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
        monkeypatch.setattr(dashboard_api, "_admin_password_snapshot", lambda: ("", ("disabled",)))
        resp = client.post("/api/v1/dashboard/auth/login", json={"password": "x"})
        assert resp.status_code == 503

    def test_logout_revokes_bearer_session(self, client):
        token = _auth_header(client)["Authorization"][7:]
        resp = client.post("/api/v1/dashboard/auth/logout",
                           headers={"Authorization": f"Bearer {token}",
                                    "Sec-Fetch-Site": "same-origin"})
        assert resp.status_code == 200
        # the bearer session must be dead immediately, not after cookie expiry
        assert not dashboard_api._session_valid(token)
        assert client.get("/api/v1/dashboard/auth/me",
                          headers={"Authorization": f"Bearer {token}"}).status_code == 401

    def test_logout_requires_auth_and_same_origin(self, client):
        assert client.post("/api/v1/dashboard/auth/logout").status_code == 401
        auth = _auth_header(client)
        assert client.post("/api/v1/dashboard/auth/logout", headers=auth).status_code == 403

    def test_logout_reports_revocation_failure(self, client, storage, monkeypatch):
        auth = _auth_header(client)
        monkeypatch.setattr(storage, "revoke_sessions", lambda token_hashes: False)
        resp = client.post(
            "/api/v1/dashboard/auth/logout",
            headers={**auth, "Sec-Fetch-Site": "same-origin"})
        assert resp.status_code == 503
        assert client.get("/api/v1/dashboard/auth/me", headers=auth).status_code == 200

    def test_logout_rejects_unavailable_password_without_reviving_session(
            self, client, monkeypatch):
        auth = _auth_header(client)
        monkeypatch.setattr(dashboard_api, "_admin_password", lambda: "")

        resp = client.post(
            "/api/v1/dashboard/auth/logout",
            headers={**auth, "Sec-Fetch-Site": "same-origin"})

        assert resp.status_code == 503
        assert client.get("/api/v1/dashboard/auth/me", headers=auth).status_code == 200

    def test_password_disable_or_rotation_invalidates_existing_session(self, client, monkeypatch):
        auth = _auth_header(client)
        monkeypatch.setattr(dashboard_api, "_admin_password_snapshot", lambda: ("", ("disabled",)))
        assert client.get("/api/v1/dashboard/auth/me", headers=auth).status_code == 401
        monkeypatch.setattr(
            dashboard_api, "_admin_password_snapshot",
            lambda: ("rotated-password", ("rotated",)))
        assert client.get("/api/v1/dashboard/auth/me", headers=auth).status_code == 401
        monkeypatch.setattr(
            dashboard_api, "_admin_password_snapshot",
            lambda: ("test-pass-123", ("restored",)))
        assert client.get("/api/v1/dashboard/auth/me", headers=auth).status_code == 401

    def test_disabled_login_purges_sessions_before_password_is_restored(self, client, monkeypatch):
        auth = _auth_header(client)
        monkeypatch.setattr(dashboard_api, "_admin_password_snapshot", lambda: ("", ("disabled",)))
        assert client.post(
            "/api/v1/dashboard/auth/login", json={"password": "anything"}).status_code == 503
        monkeypatch.setattr(
            dashboard_api, "_admin_password_snapshot",
            lambda: ("test-pass-123", ("restored",)))
        assert client.get("/api/v1/dashboard/auth/me", headers=auth).status_code == 401

    def test_cached_password_revalidates_shared_generation(self, client, storage):
        assert storage.sync_admin_password("password-a")
        first_generation = storage.admin_password_generation()
        dashboard_api._password_sync_state.update({
            "db_path": storage.db_path,
            "password": "password-a",
            "generation": first_generation,
        })
        assert storage.sync_admin_password("password-b")
        second_generation = storage.admin_password_generation()

        assert dashboard_api._sync_admin_password("password-a", ("restored",)) is True

        assert storage.admin_password_generation() == second_generation + 1
        assert dashboard_api._password_sync_state["generation"] == second_generation + 1

    def test_session_validation_uses_authoritative_file_password(self, client, monkeypatch):
        password = {"value": "password-a"}
        signature = {"value": (1,)}
        monkeypatch.setattr(
            dashboard_api, "_admin_password_snapshot",
            lambda: (password["value"], signature["value"]))
        token = __import__("asyncio").run(dashboard_api._create_session("password-a"))
        assert dashboard_api._session_valid(token) is True

        password["value"] = "password-b"
        signature["value"] = (2,)

        assert dashboard_api._session_valid(token) is False


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

        monkeypatch.setattr(dashboard_api, "TRUSTED_PROXY_HOPS", 1)
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
        import time as _time

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
            key = dashboard_api._credential_hash(
                dashboard_api._lockout_key(make_request(f"10.0.{i}.1")))
            storage.verify_login_attempt(
                key, False, _time.time(), dashboard_api.LOCKOUT_SECONDS,
                dashboard_api.MAX_FAILED_ATTEMPTS,
                dashboard_api.MAX_LOCKOUT_KEYS * dashboard_api.MAX_FAILED_ATTEMPTS)
        assert storage.login_attempt_row_count() <= 10 * dashboard_api.MAX_FAILED_ATTEMPTS
        newest = dashboard_api._credential_hash("10.0.199.1")
        decision = storage.verify_login_attempt(
            newest, False, _time.time(), dashboard_api.LOCKOUT_SECONDS,
            dashboard_api.MAX_FAILED_ATTEMPTS, 50)
        assert decision["failed_count"] == 2

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
        storage.verify_login_attempt(
            key_hash, False, _time.time() - dashboard_api.LOCKOUT_SECONDS - 1,
            dashboard_api.LOCKOUT_SECONDS, dashboard_api.MAX_FAILED_ATTEMPTS, 100)
        # the next failed login removes attempts older than the lockout window
        storage.verify_login_attempt(
            dashboard_api._credential_hash("some-other-key"), False, _time.time(),
            dashboard_api.LOCKOUT_SECONDS, dashboard_api.MAX_FAILED_ATTEMPTS, 100)
        decision = storage.verify_login_attempt(
            key_hash, False, _time.time(), dashboard_api.LOCKOUT_SECONDS,
            dashboard_api.MAX_FAILED_ATTEMPTS, 100)
        assert decision["failed_count"] == 1


class TestSameOrigin:
    @pytest.fixture(autouse=True)
    def _external_origin(self, monkeypatch):
        monkeypatch.setattr(
            dashboard_api, "DASHBOARD_EXTERNAL_ORIGIN", "https://review.achord.cn")

    def _request(self, headers, scheme="https"):
        from fastapi import Request
        from starlette.datastructures import Headers as SHeaders

        scope = {
            "type": "http", "method": "POST", "path": "/", "headers": [],
            "query_string": b"", "client": ("10.0.0.1", 1234), "server": ("h", 80),
            "scheme": scheme,
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

    def test_origin_match_passes(self, client, monkeypatch):
        monkeypatch.setattr(
            dashboard_api, "DASHBOARD_EXTERNAL_ORIGIN", "https://review.achord.cn")
        dashboard_api.require_same_origin(self._request({
            "origin": "https://review.achord.cn", "host": "review.achord.cn"}))

    def test_referer_path_match_passes(self, client, monkeypatch):
        monkeypatch.setattr(
            dashboard_api, "DASHBOARD_EXTERNAL_ORIGIN", "https://review.achord.cn")
        dashboard_api.require_same_origin(self._request({
            "referer": "https://review.achord.cn/dashboard/config?tab=model",
            "host": "review.achord.cn"}))

    def test_origin_mismatch_rejected(self, client):
        import pytest as _pytest
        from fastapi import HTTPException

        with _pytest.raises(HTTPException) as exc:
            dashboard_api.require_same_origin(self._request({
                "origin": "https://evil.example", "host": "review.achord.cn"}))
        assert exc.value.status_code == 403

    def test_cross_scheme_and_non_default_port_are_rejected(self, client, monkeypatch):
        import pytest as _pytest
        from fastapi import HTTPException

        monkeypatch.setattr(
            dashboard_api, "DASHBOARD_EXTERNAL_ORIGIN", "https://review.achord.cn")
        for origin in ("http://review.achord.cn", "https://review.achord.cn:8443"):
            with _pytest.raises(HTTPException) as exc:
                dashboard_api.require_same_origin(self._request({
                    "origin": origin, "host": "review.achord.cn"}))
            assert exc.value.status_code == 403

    def test_configured_external_origin_wins_over_internal_request_url(
            self, client, monkeypatch):
        monkeypatch.setattr(
            dashboard_api, "DASHBOARD_EXTERNAL_ORIGIN", "https://review.achord.cn")
        dashboard_api.require_same_origin(self._request({
            "origin": "https://review.achord.cn", "host": "internal:3000"}, scheme="http"))

    def test_no_evidence_rejected(self, client):
        import pytest as _pytest
        from fastapi import HTTPException

        with _pytest.raises(HTTPException) as exc:
            dashboard_api.require_same_origin(self._request({}))
        assert exc.value.status_code == 403

    def test_origin_fallback_requires_trusted_external_origin(self, client, monkeypatch):
        import pytest as _pytest
        from fastapi import HTTPException

        monkeypatch.setattr(dashboard_api, "DASHBOARD_EXTERNAL_ORIGIN", "")
        with _pytest.raises(HTTPException) as exc:
            dashboard_api.require_same_origin(self._request({
                "origin": "https://review.achord.cn", "host": "review.achord.cn"}))
        assert exc.value.status_code == 403

    def test_mutations_require_origin_evidence(self, client):
        # a bodyless cookie-authenticated POST without any origin header is now 403
        auth = _auth_header(client)
        for path in ("/api/v1/dashboard/ops/git-pull", "/api/v1/dashboard/ops/diagnose"):
            resp = client.post(path, headers=auth)
            assert resp.status_code == 403, path


class TestProtectedRoutes:
    def test_storage_singleton_is_resolved_in_worker_thread(self, client, storage, monkeypatch):
        event_loop_threads = []
        resolved_on = []

        async def allow_request(*args, **kwargs):
            event_loop_threads.append(__import__("threading").get_ident())

        def threaded_storage():
            resolved_on.append(__import__("threading").get_ident())
            return storage

        monkeypatch.setattr(dashboard_api, "require_auth", allow_request)
        monkeypatch.setattr(dashboard_api, "get_storage", threaded_storage)

        resp = client.get("/api/v1/dashboard/reviews")

        assert resp.status_code == 200
        assert event_loop_threads and resolved_on
        assert all(thread_id != event_loop_threads[0] for thread_id in resolved_on)

    def test_ops_capabilities_report_host_managed_updates(self, client, monkeypatch):
        monkeypatch.setattr(
            dashboard_api.ops, "git_pull_capability",
            lambda: {"available": False, "reason": "host managed"})

        resp = client.get(
            "/api/v1/dashboard/ops/capabilities", headers=_auth_header(client))

        assert resp.status_code == 200
        assert resp.json()["data"]["git_pull"] == {
            "available": False, "reason": "host managed"}

    def test_config_save_without_restart_reports_not_restarted(self, client):
        auth = _auth_header(client)
        resp = client.put(
            "/api/v1/dashboard/config",
            headers={**auth, "Sec-Fetch-Site": "same-origin"},
            json={"model": "openai/gpt-test", "restart": False})
        assert resp.status_code == 200
        assert resp.json()["data"]["restarted"] is False
        assert resp.json()["data"]["restart_started"] is False

    def test_config_restart_acceptance_is_not_reported_as_completion(
            self, client, storage, monkeypatch):
        monkeypatch.setattr(
            dashboard_api.ops, "restart_container",
            lambda: {"started": True, "completed": False, "exit_code": None, "output": []})
        auth = _auth_header(client)

        resp = client.put(
            "/api/v1/dashboard/config",
            headers={**auth, "Sec-Fetch-Site": "same-origin"},
            json={"model": "openai/gpt-test", "restart": True})

        assert resp.status_code == 200
        assert resp.json()["data"]["restart_started"] is True
        assert resp.json()["data"]["restarted"] is False
        assert "待确认" in resp.json()["message"]
        restart_logs = [
            row for row in storage.list_audit_logs()
            if row["action"] == "RESTART_CONTAINER"]
        assert restart_logs
        import json
        details = json.loads(restart_logs[0]["details_json"])
        assert details == {
            "source": "config_save", "started": True,
            "completed": False, "exit_code": None,
        }

    def test_config_rejects_string_restart_flag(self, client):
        auth = _auth_header(client)
        resp = client.put(
            "/api/v1/dashboard/config",
            headers={**auth, "Sec-Fetch-Site": "same-origin"},
            json={"model": "openai/gpt-test", "restart": "false"})
        assert resp.status_code == 422

    def test_config_rejects_unknown_fields(self, client):
        auth = _auth_header(client)
        resp = client.put(
            "/api/v1/dashboard/config",
            headers={**auth, "Sec-Fetch-Site": "same-origin"},
            json={"modle": "openai/typo"})
        assert resp.status_code == 400
        assert "unknown field" in resp.json()["detail"]

    def test_config_reports_persisted_but_pending_hot_reload(self, client, monkeypatch):
        class PartialEngine:
            def write(self, fields):
                return True, ["configuration saved but hot reload failed; restart required"]

        monkeypatch.setattr(dashboard_api, "get_config_engine", lambda: PartialEngine())
        auth = _auth_header(client)
        resp = client.put(
            "/api/v1/dashboard/config",
            headers={**auth, "Sec-Fetch-Site": "same-origin"},
            json={"model": "openai/persisted"})
        assert resp.status_code == 200
        assert resp.json()["data"]["hot_reload_pending"] is True
        assert "需要重启" in resp.json()["message"]

    def test_config_reports_post_rename_durability_warning(self, client, monkeypatch):
        class PartialEngine:
            def write(self, fields):
                return True, [
                    "configuration saved but directory sync failed; "
                    "crash durability unconfirmed: unsupported"
                ]

        monkeypatch.setattr(dashboard_api, "get_config_engine", lambda: PartialEngine())
        auth = _auth_header(client)
        resp = client.put(
            "/api/v1/dashboard/config",
            headers={**auth, "Sec-Fetch-Site": "same-origin"},
            json={"model": "openai/persisted"})

        assert resp.status_code == 200
        assert resp.json()["data"]["hot_reload_pending"] is False
        assert "directory sync failed" in resp.json()["data"]["persistence_warning"]
        assert "持久性同步未确认" in resp.json()["message"]

    def test_ops_reports_command_not_started(self, client, monkeypatch):
        auth = _auth_header(client)
        monkeypatch.setattr(
            dashboard_api.ops, "restart_container",
            lambda: {"started": False, "completed": True, "exit_code": None,
                     "output": ["docker unavailable"]})
        resp = client.post(
            "/api/v1/dashboard/ops/restart",
            headers={**auth, "Sec-Fetch-Site": "same-origin"})
        assert resp.status_code == 503
        assert resp.json()["code"] == "OPERATION_NOT_STARTED"
        assert resp.json()["data"]["started"] is False

    def test_git_pull_audit_records_terminal_outcome(self, client, storage, monkeypatch):
        monkeypatch.setattr(
            dashboard_api.ops, "git_pull",
            lambda: {"started": True, "completed": True, "exit_code": 7,
                     "timed_out": False, "output": ["pull rejected"]})
        auth = _auth_header(client)

        resp = client.post(
            "/api/v1/dashboard/ops/git-pull",
            headers={**auth, "Sec-Fetch-Site": "same-origin"})

        assert resp.status_code == 500
        logs = [row for row in storage.list_audit_logs() if row["action"] == "GIT_PULL"]
        assert logs
        import json
        assert json.loads(logs[0]["details_json"]) == {
            "started": True,
            "completed": True,
            "exit_code": 7,
            "timed_out": False,
        }

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

    @pytest.mark.parametrize(("method", "path"), [
        ("list_reviews", "/api/v1/dashboard/reviews"),
        ("get_review_detail", "/api/v1/dashboard/reviews/1"),
        ("list_repos", "/api/v1/dashboard/repos"),
        ("stats_overview", "/api/v1/dashboard/stats/overview"),
        ("list_audit_logs", "/api/v1/dashboard/audit-logs"),
    ])
    def test_data_routes_report_storage_failure(self, client, storage, monkeypatch, method, path):
        auth = _auth_header(client)

        def unavailable(*args, **kwargs):
            raise DashboardStorageReadError("volume unavailable")

        monkeypatch.setattr(storage, method, unavailable)
        resp = client.get(path, headers=auth)

        assert resp.status_code == 503
        assert "暂不可用" in resp.json()["detail"]

    def test_reserved_routes_come_soon(self, client):
        auth = _auth_header(client)
        for path in ["/api/v1/dashboard/commands", "/api/v1/dashboard/issues",
                     "/api/v1/dashboard/stats/hotspots", "/api/v1/dashboard/alerts/channels",
                     "/api/v1/dashboard/config/versions", "/api/v1/dashboard/webhooks"]:
            resp = client.get(path, headers=auth)
            assert resp.status_code == 501, path
            assert resp.json()["code"] == "COMING_SOON"

    def test_reserved_routes_require_auth(self, client):
        assert client.get("/api/v1/dashboard/commands").status_code == 401

    def test_unknown_route_404(self, client):
        auth = _auth_header(client)
        assert client.get("/api/v1/dashboard/nothing/here", headers=auth).status_code == 404
