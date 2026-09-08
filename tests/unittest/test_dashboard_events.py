"""Tests for the dashboard event bus storage and the SSE stream endpoint."""

import json

import pytest
from fastapi.testclient import TestClient

import pr_agent.servers.dashboard_api as dashboard_api
from pr_agent.dashboard.storage import DashboardStorage


@pytest.fixture()
def storage(tmp_path):
    store = DashboardStorage(db_path=str(tmp_path / "events_test.db"))
    store.initialize()
    return store


@pytest.fixture()
def client(storage, monkeypatch):
    monkeypatch.setattr(dashboard_api, "get_storage", lambda: storage)
    # the audit layer resolves storage through its own module-level singleton;
    # point it at the fixture store so tests can emit real events
    import pr_agent.dashboard.storage as storage_module
    monkeypatch.setattr(storage_module, "_storage", storage)
    monkeypatch.setenv("DASHBOARD_ADMIN_PASSWORD", "test-pass-123")
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(dashboard_api.router)
    return TestClient(app)


def _login_headers(client):
    resp = client.post("/api/v1/dashboard/auth/login", json={"password": "test-pass-123"})
    assert resp.status_code == 200, resp.text
    # TestClient speaks plain http, so the Secure cookie is not echoed back;
    # tests authenticate through the equivalent bearer path instead.
    return {"Authorization": f"Bearer {resp.cookies.get(dashboard_api.SESSION_COOKIE)}"}


class TestEventStorage:
    def test_add_and_list_events_in_id_order(self, storage):
        storage.add_event("review.requested", request_id="r1", review_id=1,
                          payload={"repo_name": "octo/repo", "pr_number": 7})
        storage.add_event("review.completed", request_id="r1", review_id=1,
                          payload={"repo_name": "octo/repo", "pr_number": 7, "verdict": "APPROVE"})
        events = storage.list_events()
        assert [event["id"] for event in events] == [1, 2]
        assert [event["event_type"] for event in events] == ["review.requested", "review.completed"]
        assert events[0]["payload"]["pr_number"] == 7  # numbers stay numbers
        assert events[1]["payload"]["verdict"] == "APPROVE"

    def test_list_events_after_id_resumes_the_stream(self, storage):
        for index in range(3):
            storage.add_event("review.requested", payload={"pr_number": index})
        assert [event["payload"]["pr_number"] for event in storage.list_events(after_id=1)] == [1, 2]
        assert storage.list_events(after_id=3) == []
        assert storage.latest_event_id() == 3

    def test_event_payload_text_is_bounded(self, storage):
        storage.add_event("review.reply", payload={"pr_title": "x" * 10_000})
        events = storage.list_events(limit=1)
        assert len(events[0]["payload"]["pr_title"]) <= 2003  # text + truncation marker

    def test_malformed_payload_json_degrades_to_empty_dict(self, storage):
        storage._write(
            "INSERT INTO dashboard_events (event_type, request_id, payload_json, created_at)"
            " VALUES ('review.reply', 'r1', 'not-json', '2026-01-01 00:00:00')")
        events = storage.list_events()
        assert events[0]["payload"] == {}

    def test_latest_request_id_for_pr_finds_the_most_recent_run(self, storage):
        storage.create_review(repo_name="octo/repo", pr_number=5, pr_url="u")
        second = storage.create_review(repo_name="octo/repo", pr_number=5, pr_url="u")
        assert storage.latest_request_id_for_pr("octo/repo", 5) == second
        assert storage.latest_request_id_for_pr("octo/repo", 6) is None
        assert storage.latest_request_id_for_pr("", 5) is None

    def test_event_retention_bounds_row_count(self, storage, monkeypatch):
        monkeypatch.setattr(storage_module(), "MAX_DASHBOARD_EVENT_ROWS", 2)
        for index in range(5):
            storage.add_event("review.requested", payload={"pr_number": index})
        storage.reconcile_stale_reviews(force=True)
        events = storage.list_events()
        assert len(events) == 2
        assert events[-1]["payload"]["pr_number"] == 4


def storage_module():
    import pr_agent.dashboard.storage as module
    return module


class TestEventsStream:
    def test_stream_requires_auth(self, client):
        assert client.get("/api/v1/dashboard/events/stream").status_code == 401

    def test_stream_emits_events_as_sse_frames(self, client, storage, monkeypatch):
        # a fast clock and instant idle make the generator drain existing
        # events and exit after one poll — the real stream runs forever.
        # Events come from the audit layer (the production emitter), not from
        # raw storage calls.
        monkeypatch.setattr(dashboard_api, "SSE_POLL_SECONDS", 0)
        monkeypatch.setattr(dashboard_api, "SSE_STREAM_LIMIT_SECONDS", 0)
        monkeypatch.setenv("DASHBOARD_DB_PATH", storage.db_path)
        from pr_agent.dashboard import audit

        audit.review_started("https://github.com/octo/repo/pull/42",
                             sender="alice", pr_title="Test PR")
        headers = _login_headers(client)
        response = client.get("/api/v1/dashboard/events/stream", headers=headers)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-accel-buffering"] == "no"
        # the leading empty chunk flushes headers through proxies early;
        # the first real frame is the first non-empty split segment
        frames = [frame for frame in response.text.split("\n\n") if frame.strip()]
        assert frames[0].split("\n")[0].startswith("id: ")
        assert "event: dashboard" in frames[0]
        data_line = next(line for line in frames[0].split("\n") if line.startswith("data: "))
        payload = json.loads(data_line[len("data: "):])
        assert payload["event_type"] == "review.requested"
        assert payload["repo_name"] == "octo/repo"
        assert payload["pr_number"] == 42

    def test_stream_resumes_from_query_parameter(self, client, storage, monkeypatch):
        monkeypatch.setattr(dashboard_api, "SSE_POLL_SECONDS", 0)
        monkeypatch.setattr(dashboard_api, "SSE_STREAM_LIMIT_SECONDS", 0)
        monkeypatch.setenv("DASHBOARD_DB_PATH", storage.db_path)
        from pr_agent.dashboard import audit

        audit.review_started("https://github.com/octo/repo/pull/1")
        audit.review_started("https://github.com/octo/repo/pull/2")
        headers = _login_headers(client)
        response = client.get("/api/v1/dashboard/events/stream?lastEventId=1", headers=headers)
        first_frame = next(frame for frame in response.text.split("\n\n") if frame.strip())
        payload = json.loads(first_frame.split("data: ", 1)[1].split("\n", 1)[0])
        # the first frame after the resume cursor is event 2, not 1
        assert payload["pr_number"] == 2

    def test_stream_survives_a_storage_read_outage(self, client, storage, monkeypatch):
        monkeypatch.setattr(dashboard_api, "SSE_POLL_SECONDS", 0)
        monkeypatch.setattr(dashboard_api, "SSE_STREAM_LIMIT_SECONDS", 0)
        storage.add_event("review.requested")
        headers = _login_headers(client)

        def _failing_read(*args, **kwargs):
            from pr_agent.dashboard.storage import DashboardStorageReadError
            raise DashboardStorageReadError("dashboard storage read failed")

        monkeypatch.setattr(storage, "list_events", _failing_read)
        response = client.get("/api/v1/dashboard/events/stream", headers=headers)
        # the connection completes without erroring and emits no events
        assert response.status_code == 200
        assert "data:" not in response.text
