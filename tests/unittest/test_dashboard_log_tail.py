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


def test_multiline_message_with_interior_timestamp_stays_in_the_review(log_base):
    # An interior line that merely starts with a timestamp (a traceback, an echoed
    # log line) is NOT a new record — it must stay with the review it belongs to,
    # not get split off and dropped by the per-review filter.
    _write(log_base.parent / "achord-review.777.log", [
        "2026-09-07 12:00:00.000 | ERROR   | reqAAA | m:f:1 - build failed:",
        "2026-09-07 12:00:00 deployment failed on host X",  # looks like a timestamp, no header
        "    stack frame 2",
        "2026-09-07 12:00:05.000 | INFO    | reqBBB | m:f:2 - unrelated line",
    ])
    lines = ops.tail_logs_for_request("reqAAA")
    assert any("build failed:" in ln for ln in lines)
    assert "2026-09-07 12:00:00 deployment failed on host X" in lines
    assert "    stack frame 2" in lines
    assert not any("unrelated line" in ln for ln in lines)


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


def test_tail_includes_the_configured_base_file(log_base):
    # A deployment (or a pre-upgrade log) that points the env at a literal file is
    # still read, alongside the per-pid worker files.
    _write(log_base, ["2026-09-07 12:00:05.000 | INFO    | - | m:f:1 - from the base file"])
    _write(log_base.parent / f"achord-review.{os.getpid()}.log",
           ["2026-09-07 12:00:00.000 | INFO    | - | m:f:1 - from a worker file"])
    lines = ops.tail_logs()
    assert any("from the base file" in ln for ln in lines)
    assert any("from a worker file" in ln for ln in lines)


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


def test_glob_and_prune_match_numbered_rotation_backups(log_base, monkeypatch):
    # RotatingFileHandler names rollovers "<...>.log.1"; they must be discoverable
    # and prunable, not just the current ".log" file.
    monkeypatch.setattr(ops, "_process_alive", lambda pid: False)
    monkeypatch.setattr(ops, "MAX_LOG_FILES_KEPT", 0)  # any dead-owner file is prunable
    current = log_base.parent / "achord-review.4242.log"
    backup = log_base.parent / "achord-review.4242.log.1"
    _write(current, ["2026-09-07 12:00:01.000 | INFO    | - | m:f:1 - current"])
    _write(backup, ["2026-09-07 12:00:00.000 | INFO    | - | m:f:1 - rolled over"])
    files, _stem = ops._glob_review_log_files()
    names = {os.path.basename(p) for p in files}
    assert {"achord-review.4242.log", "achord-review.4242.log.1"} <= names
    ops.prune_review_log_files()
    assert not current.exists() and not backup.exists()


def test_writer_rollover_creates_a_numbered_backup_the_reader_sees(log_base, monkeypatch):
    import pr_agent.log as logmod
    from pr_agent.log import _ReviewLogWriter

    monkeypatch.setattr(logmod, "REVIEW_LOG_ROTATION_BYTES", 2000)  # tiny — force real rollovers
    stem, ext = os.path.splitext(str(log_base))
    per_pid = f"{stem}.{os.getpid()}{ext}"
    os.makedirs(os.path.dirname(per_pid), exist_ok=True)  # enable_review_log_sink does this in prod
    writer = _ReviewLogWriter(per_pid)
    try:
        for i in range(300):
            writer.write(
                f"2026-09-07 12:00:{i % 60:02d}.000 | INFO    | - | m:f:1 - line {i} "
                f"{'padding' * 5}\n")
    finally:
        writer.stop()  # drain + flush all rollovers before asserting
    assert os.path.isfile(per_pid + ".1")  # RotatingFileHandler produced a numbered backup
    files, _stem = ops._glob_review_log_files()
    assert any(os.path.basename(p) == os.path.basename(per_pid) + ".1" for p in files)


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
        logmod._REVIEW_LOG_WRITER.stop()  # drain + stop the writer thread before reading
        stem, ext = os.path.splitext(str(log_base))
        expected = f"{stem}.{os.getpid()}{ext}"
        assert os.path.isfile(expected)
        content = open(expected, encoding="utf-8").read()
        assert "req12345" in content and "hello inside a review" in content
    finally:
        # Detach the sink and clear the default extra so nothing points at this tmp
        # file (deleted on teardown) or leaks into other tests' logging.
        get_logger().remove(sink_id)
        logmod._REVIEW_SINK_ID = None
        logmod._REVIEW_LOG_WRITER = None
        get_logger().configure(extra={})


def test_removing_the_sink_drains_the_writer(log_base):
    # loguru wraps the writer object as a stream sink and calls stop() on remove
    # (and at exit), so a graceful shutdown flushes the queue instead of dropping
    # the final lines with the daemon thread.
    import pr_agent.log as logmod
    from pr_agent.log import enable_review_log_sink, get_logger

    sink_id = enable_review_log_sink()
    writer = logmod._REVIEW_LOG_WRITER
    try:
        with get_logger().contextualize(review_request_id="reqZZZ"):
            get_logger().info("line before shutdown")
        get_logger().remove(sink_id)  # must drain + stop the writer via StreamSink.stop()
        assert not writer._thread.is_alive()
        stem, ext = os.path.splitext(str(log_base))
        content = open(f"{stem}.{os.getpid()}{ext}", encoding="utf-8").read()
        assert "line before shutdown" in content
    finally:
        logmod._REVIEW_SINK_ID = None
        logmod._REVIEW_LOG_WRITER = None
        get_logger().configure(extra={})


def test_review_log_writer_drops_instead_of_blocking_when_full(tmp_path):
    import queue as _queue

    from pr_agent.log import _ReviewLogWriter

    writer = _ReviewLogWriter(str(tmp_path / "x.log"))
    writer.stop()  # stop the drain thread so the queue cannot empty
    # A full bounded queue with no consumer: write must drop, never block.
    writer._queue = _queue.Queue(maxsize=1)
    writer._queue.put_nowait("occupied")
    writer.write("dropped-1")
    writer.write("dropped-2")
    assert writer.dropped == 2
