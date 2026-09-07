"""Tests for the dashboard log file sink and per-review log tailing.

Reviews only logged to stdout (readable on the host, not in-container), so the
panel's log view was empty. These cover the per-pid file sink and the merge/grep
readers that back the ops log console and a review's own log view.
"""

import os

import pytest

from pr_agent.dashboard import ops


@pytest.fixture
def log_base(monkeypatch, tmp_path):
    """Point the sink/readers at an isolated logs directory and return the base path."""
    base = tmp_path / "logs" / "achord-review.log"
    monkeypatch.setenv("ACHORD_REVIEW_LOG_FILE", str(base))
    return base


def _write(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_no_env_returns_no_files_and_empty_tail(monkeypatch):
    monkeypatch.delenv("ACHORD_REVIEW_LOG_FILE", raising=False)
    assert ops._review_log_files() == []
    assert ops.tail_logs() == []
    assert ops.tail_logs_for_request("anything") == []


def test_split_records_groups_multiline_messages():
    text = (
        "2026-09-07 12:00:00.100 | INFO    | abc | m:f:1 - line one\n"
        "    continued body\n"
        "2026-09-07 12:00:01.200 | INFO    | abc | m:f:2 - line two\n"
    )
    records = ops._split_records(text)
    assert [r[0] for r in records] == ["2026-09-07 12:00:00.100", "2026-09-07 12:00:01.200"]
    assert records[0][1] == [
        "2026-09-07 12:00:00.100 | INFO    | abc | m:f:1 - line one",
        "    continued body",
    ]


def test_tail_merges_across_worker_files_by_timestamp(log_base):
    stem, ext = os.path.splitext(str(log_base))
    _write(log_base.parent / f"{os.path.basename(stem)}.111{ext}", [
        "2026-09-07 12:00:00.000 | INFO    | - | m:f:1 - worker one A",
        "2026-09-07 12:00:02.000 | INFO    | - | m:f:1 - worker one B",
    ])
    _write(log_base.parent / f"{os.path.basename(stem)}.222{ext}", [
        "2026-09-07 12:00:01.000 | INFO    | - | m:f:1 - worker two A",
    ])
    lines = ops.tail_logs()
    # Interleaved into real chronological order regardless of which file they sat in.
    assert lines == [
        "2026-09-07 12:00:00.000 | INFO    | - | m:f:1 - worker one A",
        "2026-09-07 12:00:01.000 | INFO    | - | m:f:1 - worker two A",
        "2026-09-07 12:00:02.000 | INFO    | - | m:f:1 - worker one B",
    ]


def test_tail_returns_the_most_recent_lines_up_to_the_cap(log_base):
    _write(log_base.parent / "achord-review.111.log", [
        f"2026-09-07 12:00:{n:02d}.000 | INFO    | - | m:f:1 - line {n}" for n in range(10)
    ])
    lines = ops.tail_logs(max_lines=3)
    assert lines == [
        "2026-09-07 12:00:07.000 | INFO    | - | m:f:1 - line 7",
        "2026-09-07 12:00:08.000 | INFO    | - | m:f:1 - line 8",
        "2026-09-07 12:00:09.000 | INFO    | - | m:f:1 - line 9",
    ]


def test_per_request_filter_keeps_only_that_review_including_continuations(log_base):
    _write(log_base.parent / "achord-review.111.log", [
        "2026-09-07 12:00:00.000 | INFO    | reqAAA | m:f:1 - start A",
        "    A body continues without the id",
        "2026-09-07 12:00:01.000 | INFO    | reqBBB | m:f:1 - start B",
        "2026-09-07 12:00:02.000 | INFO    | reqAAA | m:f:1 - end A",
    ])
    lines = ops.tail_logs_for_request("reqAAA")
    assert lines == [
        "2026-09-07 12:00:00.000 | INFO    | reqAAA | m:f:1 - start A",
        "    A body continues without the id",
        "2026-09-07 12:00:02.000 | INFO    | reqAAA | m:f:1 - end A",
    ]
    assert ops.tail_logs_for_request("") == []


def test_review_log_files_prune_oldest_dead_owner_beyond_the_cap(log_base, monkeypatch):
    monkeypatch.setattr(ops, "MAX_LOG_FILES_KEPT", 3)
    monkeypatch.setattr(ops, "MAX_LOG_FILES_SCANNED", 3)
    monkeypatch.setattr(ops, "_process_alive", lambda pid: False)  # all owners exited
    created = []
    for pid in range(1001, 1007):
        path = log_base.parent / f"achord-review.{pid}.log"
        _write(path, [f"2026-09-07 12:00:00.000 | INFO | - | m:f:1 - {pid}"])
        os.utime(path, (pid, pid))  # ascending mtime: higher pid is newer
        created.append(path)
    ops._review_log_files()
    survivors = {os.path.basename(p) for p in created if p.exists()}
    assert survivors == {"achord-review.1004.log", "achord-review.1005.log", "achord-review.1006.log"}


def test_review_log_files_never_prunes_a_live_owner_file(log_base, monkeypatch):
    monkeypatch.setattr(ops, "MAX_LOG_FILES_KEPT", 1)
    monkeypatch.setattr(ops, "MAX_LOG_FILES_SCANNED", 10)
    # 9999 is a live worker; everyone else has exited.
    monkeypatch.setattr(ops, "_process_alive", lambda pid: pid == 9999)
    live = log_base.parent / "achord-review.9999.log"
    _write(live, ["2026-09-07 11:00:00.000 | INFO | - | m:f:1 - live but idle"])
    os.utime(live, (1, 1))  # oldest by far — would be pruned if ownership were ignored
    for pid in range(2001, 2006):
        path = log_base.parent / f"achord-review.{pid}.log"
        _write(path, [f"2026-09-07 12:00:00.000 | INFO | - | m:f:1 - {pid}"])
        os.utime(path, (pid, pid))
    ops._review_log_files()
    assert live.exists()  # the live worker's file is kept despite being the oldest


def test_enable_review_log_sink_writes_a_per_pid_file_carrying_the_review_id(log_base, monkeypatch):
    import pr_agent.log as logmod
    from pr_agent.log import enable_review_log_sink, get_logger

    sink_id = enable_review_log_sink()
    assert sink_id is not None
    try:
        with get_logger().contextualize(review_request_id="req12345"):
            get_logger().info("hello inside a review")
        get_logger().complete()  # drain the enqueued writer thread before reading
        stem, ext = os.path.splitext(str(log_base))
        expected = f"{stem}.{os.getpid()}{ext}"
        assert os.path.isfile(expected)
        content = open(expected, encoding="utf-8").read()
        assert "req12345" in content and "hello inside a review" in content
    finally:
        # Detach the enqueued sink (joins its writer thread — safe in-process) and
        # clear the default extra so nothing points at this tmp file (deleted on
        # teardown) or leaks into other tests' logging.
        get_logger().remove(sink_id)
        logmod._REVIEW_SINK_ID = None
        get_logger().configure(extra={})
