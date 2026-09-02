"""
Dashboard persistence on a single SQLite file.

The database is an isolated local file (/app/data/review.db in the container):
no external Postgres/Redis connection, zero coupling with anything else on the
host. Each process serializes its own writers and SQLite coordinates access
between gunicorn workers through WAL/file locking. Public entry points swallow
storage errors where the caller can safely degrade, so dashboard persistence
must never break the webhook review flow.
"""

import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from pr_agent.log import get_logger

DEFAULT_DB_PATH = os.environ.get("DASHBOARD_DB_PATH", "/app/data/review.db")
STALE_REVIEW_SECONDS = int(os.environ.get("DASHBOARD_STALE_REVIEW_SECONDS", str(6 * 3600)))
STALE_CLEANUP_INTERVAL_SECONDS = 5 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT UNIQUE NOT NULL,
    repo_name TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    pr_title TEXT,
    pr_url TEXT NOT NULL,
    commit_sha TEXT,
    sender TEXT,
    trigger_type TEXT DEFAULT 'mention',
    command TEXT DEFAULT '/review',

    status TEXT NOT NULL,
    verdict TEXT,
    verdict_reason TEXT,

    model TEXT,
    reasoning_effort TEXT,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,

    raw_prediction TEXT,
    markdown_output TEXT,
    error_message TEXT,

    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS review_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    severity TEXT,
    relevant_file TEXT,
    relevant_lines_start INTEGER,
    relevant_lines_end INTEGER,
    issue_summary TEXT,
    suggestion TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operator TEXT DEFAULT 'admin',
    action TEXT NOT NULL,
    details_json TEXT,
    ip_address TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dashboard_sessions (
    token_hash TEXT PRIMARY KEY,
    expires_at INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dashboard_login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lockout_key TEXT NOT NULL,
    attempted_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reviews_repo_pr ON reviews(repo_name, pr_number);
CREATE INDEX IF NOT EXISTS idx_reviews_status ON reviews(status);
CREATE INDEX IF NOT EXISTS idx_reviews_created_at ON reviews(created_at);
CREATE INDEX IF NOT EXISTS idx_issues_severity ON review_issues(severity);
CREATE INDEX IF NOT EXISTS idx_issues_review ON review_issues(review_id);
CREATE INDEX IF NOT EXISTS idx_dashboard_sessions_expiry ON dashboard_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_dashboard_login_attempts_key_time
    ON dashboard_login_attempts(lockout_key, attempted_at);
CREATE INDEX IF NOT EXISTS idx_dashboard_login_attempts_time ON dashboard_login_attempts(attempted_at);
"""

_MAX_RETRY = 3


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _utc_at(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class DashboardStorage:
    """Thread-safe SQLite access with single-writer serialization and WAL reads."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._write_lock = threading.Lock()
        self._stale_cleanup_lock = threading.Lock()
        self._last_stale_cleanup = 0.0

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._protect_storage_permissions()
            return conn
        except Exception:
            conn.close()
            raise

    def initialize(self) -> None:
        directory = os.path.dirname(self.db_path) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        with self._write_lock, self._connect() as conn:
            conn.executescript(_SCHEMA)
            cutoff = _utc_at(time.time() - STALE_REVIEW_SECONDS)
            conn.execute(
                "UPDATE reviews SET status='FAILED',"
                " error_message='审查进程未正常结束（服务重启或 worker 中断）', completed_at=?"
                " WHERE status='RUNNING' AND created_at < ?",
                (_utcnow(), cutoff))
        self._protect_storage_permissions()
        self._last_stale_cleanup = time.monotonic()

    def _protect_storage_permissions(self) -> None:
        """Keep the database and SQLite sidecars owner-readable only."""
        for path in (self.db_path, f"{self.db_path}-wal", f"{self.db_path}-shm"):
            if os.path.exists(path):
                os.chmod(path, 0o600)

    def _write(self, sql: str, params: tuple = ()) -> Optional[int]:
        for attempt in range(_MAX_RETRY):
            try:
                with self._write_lock, self._connect() as conn:
                    cursor = conn.execute(sql, params)
                    lastrowid = cursor.lastrowid
                self._protect_storage_permissions()
                return lastrowid
            except sqlite3.OperationalError as e:
                # "database is locked" under momentary contention; back off and retry
                if "locked" not in str(e) or attempt == _MAX_RETRY - 1:
                    get_logger().warning(f"Dashboard storage write failed, error: {e}")
                    return None
                time.sleep(0.2 * (attempt + 1))
            except Exception as e:
                get_logger().warning(f"Dashboard storage write failed, error: {e}")
                return None
        return None

    def _read(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
            self._protect_storage_permissions()
            return [dict(row) for row in rows]
        except Exception as e:
            get_logger().warning(f"Dashboard storage read failed, error: {e}")
            return []

    def _transaction(self, operation: Callable[[sqlite3.Connection], None], label: str) -> bool:
        """Run a multi-statement write with the same bounded lock retry as _write."""
        for attempt in range(_MAX_RETRY):
            try:
                with self._write_lock, self._connect() as conn:
                    operation(conn)
                self._protect_storage_permissions()
                return True
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) or attempt == _MAX_RETRY - 1:
                    get_logger().warning(f"Dashboard storage {label} failed, error: {e}")
                    return False
                time.sleep(0.2 * (attempt + 1))
            except Exception as e:
                get_logger().warning(f"Dashboard storage {label} failed, error: {e}")
                return False
        return False

    # ---------------------------------------------------------- auth state

    def create_session(self, token_hash: str, expires_at: int) -> bool:
        """Persist a dashboard session so every gunicorn worker can validate it."""
        self._write("DELETE FROM dashboard_sessions WHERE expires_at <= ?", (int(time.time()),))
        return self._write(
            "INSERT OR REPLACE INTO dashboard_sessions (token_hash, expires_at, created_at)"
            " VALUES (?, ?, ?)",
            (token_hash, expires_at, _utcnow())) is not None

    def session_is_valid(self, token_hash: str, now: Optional[int] = None) -> bool:
        rows = self._read(
            "SELECT 1 AS valid FROM dashboard_sessions WHERE token_hash = ? AND expires_at > ? LIMIT 1",
            (token_hash, int(time.time()) if now is None else now))
        return bool(rows)

    def revoke_session(self, token_hash: str) -> None:
        self._write("DELETE FROM dashboard_sessions WHERE token_hash = ?", (token_hash,))

    def verify_login_attempt(self, lockout_key: str, password_matches: bool,
                             attempted_at: float, window_seconds: int,
                             max_attempts: int, max_rows: int) -> Dict[str, Any]:
        """Atomically enforce lockout and record/clear attempts across workers."""
        decision = {"authenticated": False, "locked_out": False,
                    "failed_count": 0, "storage_error": True}

        def _verify(conn: sqlite3.Connection) -> None:
            conn.execute("BEGIN IMMEDIATE")
            cutoff = attempted_at - window_seconds
            conn.execute("DELETE FROM dashboard_login_attempts WHERE attempted_at < ?", (cutoff,))
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM dashboard_login_attempts"
                " WHERE lockout_key = ? AND attempted_at >= ?",
                (lockout_key, cutoff)).fetchone()
            failed_count = row["c"] if row else 0
            if failed_count >= max_attempts:
                decision.update({"locked_out": True, "failed_count": failed_count})
                return
            if password_matches:
                conn.execute("DELETE FROM dashboard_login_attempts WHERE lockout_key = ?", (lockout_key,))
                decision.update({"authenticated": True, "failed_count": 0})
                return
            conn.execute(
                "INSERT INTO dashboard_login_attempts (lockout_key, attempted_at) VALUES (?, ?)",
                (lockout_key, attempted_at))
            failed_count += 1
            total = conn.execute("SELECT COUNT(*) AS c FROM dashboard_login_attempts").fetchone()["c"]
            excess = max(0, total - max_rows)
            if excess:
                conn.execute(
                    "DELETE FROM dashboard_login_attempts WHERE id IN"
                    " (SELECT id FROM dashboard_login_attempts ORDER BY attempted_at, id LIMIT ?)",
                    (excess,))
            decision["failed_count"] = failed_count

        if self._transaction(_verify, "login-attempt transaction"):
            decision["storage_error"] = False
        return decision

    def login_attempt_row_count(self) -> int:
        rows = self._read("SELECT COUNT(*) AS c FROM dashboard_login_attempts")
        return rows[0]["c"] if rows else 0

    def reconcile_stale_reviews(self, force: bool = False) -> None:
        """Periodically close crashed/interrupted runs after their grace period."""
        now = time.monotonic()
        with self._stale_cleanup_lock:
            if not force and now - self._last_stale_cleanup < STALE_CLEANUP_INTERVAL_SECONDS:
                return
            cutoff = _utc_at(time.time() - STALE_REVIEW_SECONDS)

            def _reconcile(conn: sqlite3.Connection) -> None:
                conn.execute(
                    "UPDATE reviews SET status='FAILED',"
                    " error_message='审查进程未正常结束（服务重启或 worker 中断）', completed_at=?"
                    " WHERE status='RUNNING' AND created_at < ?",
                    (_utcnow(), cutoff))

            if self._transaction(_reconcile, "stale-review reconciliation"):
                self._last_stale_cleanup = now

    # ------------------------------------------------------------------ reviews

    def create_review(self, repo_name: str, pr_number: int, pr_url: str, command: str = "/review",
                      pr_title: str = "", sender: str = "", trigger_type: str = "manual",
                      commit_sha: str = "", model: str = "", reasoning_effort: str = "") -> str:
        """Insert a RUNNING record and return its request_id, or "" on failure."""
        request_id = uuid.uuid4().hex
        self._write(
            "INSERT INTO reviews (request_id, repo_name, pr_number, pr_title, pr_url, commit_sha,"
            " sender, trigger_type, command, status, model, reasoning_effort, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'RUNNING', ?, ?, ?)",
            (request_id, repo_name, pr_number, pr_title, pr_url, commit_sha, sender,
             trigger_type, command, model, reasoning_effort, _utcnow()))
        return request_id

    def complete_review(self, request_id: str, verdict: str = "", verdict_reason: str = "",
                        markdown_output: str = "", raw_prediction: str = "") -> None:
        row = self.get_review_by_request_id(request_id, summary_only=False)
        details = row or {}
        self._write(
            "UPDATE reviews SET status='COMPLETED', verdict=?, verdict_reason=?,"
            " markdown_output=?, raw_prediction=?,"
            " prompt_tokens=?, completion_tokens=?, total_tokens=?, duration_ms=?,"
            " model=COALESCE(NULLIF(?, ''), model), completed_at=? WHERE request_id=?",
            (verdict, verdict_reason, markdown_output, raw_prediction,
             details.get("prompt_tokens", 0), details.get("completion_tokens", 0),
             details.get("total_tokens", 0), details.get("duration_ms", 0),
             details.get("model", ""), _utcnow(), request_id))

    def fail_review(self, request_id: str, error_message: str) -> None:
        row = self.get_review_by_request_id(request_id, summary_only=False)
        details = row or {}
        self._write(
            "UPDATE reviews SET status='FAILED', error_message=?,"
            " prompt_tokens=?, completion_tokens=?, total_tokens=?, duration_ms=?,"
            " model=COALESCE(NULLIF(?, ''), model), completed_at=? WHERE request_id=?",
            (error_message, details.get("prompt_tokens", 0), details.get("completion_tokens", 0),
             details.get("total_tokens", 0), details.get("duration_ms", 0),
             details.get("model", ""), _utcnow(), request_id))

    def skip_review(self, request_id: str, reason: str) -> None:
        """Close a RUNNING record that exited before publishing (no files,
        incremental gate, empty model output). Distinct from FAILED so a
        genuine model/transport error stays distinguishable in the history."""
        row = self.get_review_by_request_id(request_id, summary_only=False)
        details = row or {}
        self._write(
            "UPDATE reviews SET status='SKIPPED', error_message=?,"
            " prompt_tokens=?, completion_tokens=?, total_tokens=?, duration_ms=?,"
            " model=COALESCE(NULLIF(?, ''), model), completed_at=? WHERE request_id=?",
            (reason, details.get("prompt_tokens", 0), details.get("completion_tokens", 0),
             details.get("total_tokens", 0), details.get("duration_ms", 0),
             details.get("model", ""), _utcnow(), request_id))

    def set_review_usage(self, request_id: str, model: str = "", reasoning_effort: str = "",
                         prompt_tokens: int = 0, completion_tokens: int = 0, total_tokens: int = 0,
                         duration_ms: int = 0) -> None:
        self._write(
            "UPDATE reviews SET model=COALESCE(NULLIF(?, ''), model),"
            " reasoning_effort=COALESCE(NULLIF(?, ''), reasoning_effort),"
            " prompt_tokens=?, completion_tokens=?, total_tokens=?, duration_ms=?"
            " WHERE request_id=?",
            (model, reasoning_effort, prompt_tokens, completion_tokens, total_tokens,
             duration_ms, request_id))

    def add_review_issues(self, request_id: str, issues: List[Dict[str, Any]]) -> None:
        row = self.get_review_by_request_id(request_id)
        if not row or not issues:
            return
        review_id = row["id"]
        now = _utcnow()
        for issue in issues:
            self._write(
                "INSERT INTO review_issues (review_id, severity, relevant_file,"
                " relevant_lines_start, relevant_lines_end, issue_summary, suggestion, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (review_id, issue.get("severity"), issue.get("relevant_file"),
                 issue.get("relevant_lines_start"), issue.get("relevant_lines_end"),
                 issue.get("issue_summary"), issue.get("suggestion"), now))

    def finish_review(self, request_id: str, issues: List[Dict[str, Any]], verdict: str = "",
                      verdict_reason: str = "", markdown_output: str = "",
                      raw_prediction: str = "", model: str = "", reasoning_effort: str = "",
                      prompt_tokens: int = 0, completion_tokens: int = 0,
                      total_tokens: int = 0, duration_ms: int = 0) -> None:
        """Atomically persist usage, findings and the terminal COMPLETED state.

        One transaction so a reader that sees status=COMPLETED also sees every
        finding — the detail page polls on status and would otherwise render a
        permanent empty/partial finding list for a review finished mid-write.
        Usage columns accept the run's live values; a stored value wins when
        the incoming one is empty/zero.
        """
        def _finish(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE reviews SET status='COMPLETED', verdict=?, verdict_reason=?,"
                " markdown_output=?, raw_prediction=?,"
                " model=COALESCE(NULLIF(?, ''), model),"
                " reasoning_effort=COALESCE(NULLIF(?, ''), reasoning_effort),"
                " prompt_tokens=CASE WHEN ? > 0 THEN ? ELSE prompt_tokens END,"
                " completion_tokens=CASE WHEN ? > 0 THEN ? ELSE completion_tokens END,"
                " total_tokens=CASE WHEN ? > 0 THEN ? ELSE total_tokens END,"
                " duration_ms=CASE WHEN ? > 0 THEN ? ELSE duration_ms END,"
                " completed_at=? WHERE request_id=?",
                (verdict, verdict_reason, markdown_output, raw_prediction,
                 model, reasoning_effort,
                 prompt_tokens, prompt_tokens, completion_tokens, completion_tokens,
                 total_tokens, total_tokens, duration_ms, duration_ms,
                 _utcnow(), request_id))
            row = conn.execute("SELECT id FROM reviews WHERE request_id = ?",
                               (request_id,)).fetchone()
            if row is not None and issues:
                now = _utcnow()
                conn.executemany(
                    "INSERT INTO review_issues (review_id, severity, relevant_file,"
                    " relevant_lines_start, relevant_lines_end, issue_summary, suggestion, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(row["id"], issue.get("severity"), issue.get("relevant_file"),
                      issue.get("relevant_lines_start"), issue.get("relevant_lines_end"),
                      issue.get("issue_summary"), issue.get("suggestion"), now)
                     for issue in issues])

        self._transaction(_finish, "finish-review transaction")

    def get_review_by_request_id(self, request_id: str, summary_only: bool = False) -> Optional[Dict[str, Any]]:
        columns = "id, repo_name, pr_number" if summary_only else "*"
        rows = self._read(f"SELECT {columns} FROM reviews WHERE request_id = ?", (request_id,))
        return rows[0] if rows else None

    def list_reviews(self, repo: str = "", status: str = "", verdict: str = "",
                     trigger_type: str = "", limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        self.reconcile_stale_reviews()
        where, params = [], []
        if repo:
            where.append("repo_name = ?")
            params.append(repo)
        if status:
            where.append("status = ?")
            params.append(status)
        if verdict:
            where.append("verdict = ?")
            params.append(verdict)
        if trigger_type:
            where.append("trigger_type = ?")
            params.append(trigger_type)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self._read(
            f"SELECT id, request_id, repo_name, pr_number, pr_title, pr_url, commit_sha, sender,"
            f" trigger_type, command, status, verdict, model, reasoning_effort,"
            f" prompt_tokens, completion_tokens, total_tokens, duration_ms, error_message,"
            f" created_at, completed_at FROM reviews {clause}"
            f" ORDER BY created_at DESC LIMIT ? OFFSET ?",
            tuple(params) + (limit, offset))
        total_rows = self._read(f"SELECT COUNT(*) AS c FROM reviews {clause}", tuple(params))
        # severity distribution for the listed reviews, in a single pass
        issues: Dict[int, Dict[str, int]] = {}
        if rows:
            ids = [str(row["id"]) for row in rows]
            placeholders = ",".join("?" * len(ids))
            for issue_row in self._read(
                    f"SELECT review_id, severity, COUNT(*) AS c FROM review_issues"
                    f" WHERE review_id IN ({placeholders}) GROUP BY review_id, severity",
                    tuple(ids)):
                bucket = issues.setdefault(issue_row["review_id"], {})
                bucket[issue_row["severity"] or "?"] = issue_row["c"]
        for row in rows:
            row["severity_counts"] = issues.get(row["id"], {})
        return {"total": total_rows[0]["c"] if total_rows else 0, "items": rows}

    def get_review_detail(self, review_id: int) -> Optional[Dict[str, Any]]:
        self.reconcile_stale_reviews()
        rows = self._read("SELECT * FROM reviews WHERE id = ?", (review_id,))
        if not rows:
            return None
        detail = rows[0]
        detail["issues"] = self._read(
            "SELECT id, severity, relevant_file, relevant_lines_start, relevant_lines_end,"
            " issue_summary, suggestion FROM review_issues WHERE review_id = ?"
            " ORDER BY id", (review_id,))
        return detail

    def list_repos(self) -> List[Dict[str, Any]]:
        return self._read(
            "SELECT repo_name, COUNT(*) AS review_count, MAX(created_at) AS last_review_at"
            " FROM reviews GROUP BY repo_name ORDER BY review_count DESC")

    # ------------------------------------------------------------------- stats

    def stats_overview(self) -> Dict[str, Any]:
        self.reconcile_stale_reviews()
        today = _utcnow()[:10]
        overview: Dict[str, Any] = {}
        single = self._read(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN created_at LIKE ? THEN 1 ELSE 0 END) AS today,"
            " SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed,"
            " SUM(CASE WHEN status = 'RUNNING' THEN 1 ELSE 0 END) AS running,"
            " AVG(CASE WHEN status = 'COMPLETED' THEN duration_ms END) AS avg_duration_ms,"
            " SUM(prompt_tokens) AS prompt_tokens,"
            " SUM(completion_tokens) AS completion_tokens,"
            " SUM(total_tokens) AS total_tokens FROM reviews",
            (f"{_utcnow()[:10]}%",))
        overview.update(single[0] if single else {})
        blocking = self._read(
            "SELECT COUNT(*) AS c FROM review_issues WHERE severity IN ('P0', 'P1')")
        overview["p0_p1_blocked"] = blocking[0]["c"] if blocking else 0
        severity = self._read(
            "SELECT severity, COUNT(*) AS c FROM review_issues GROUP BY severity ORDER BY severity")
        overview["severity_distribution"] = {row["severity"] or "?": row["c"] for row in severity}
        trend = self._read(
            "SELECT created_at FROM reviews WHERE created_at >= DATE('now', '-13 days')"
            " ORDER BY created_at")
        daily: Dict[str, Dict[str, int]] = {}
        for row in trend:
            day = row["created_at"][:10]
            bucket = daily.setdefault(day, {"count": 0, "tokens": 0})
            bucket["count"] += 1
        token_rows = self._read(
            "SELECT substr(created_at, 1, 10) AS day, SUM(total_tokens) AS tokens FROM reviews"
            " WHERE created_at >= DATE('now', '-13 days') GROUP BY day")
        for row in token_rows:
            daily.setdefault(row["day"], {"count": 0, "tokens": 0})["tokens"] = row["tokens"] or 0
        overview["daily_trend"] = daily
        overview["generated_for_date"] = today
        return overview

    # -------------------------------------------------------------- audit logs

    def add_audit_log(self, action: str, details: Optional[Dict[str, Any]] = None,
                      ip_address: str = "", operator: str = "admin") -> None:
        self._write(
            "INSERT INTO audit_logs (operator, action, details_json, ip_address, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (operator, action, json.dumps(details or {}, ensure_ascii=False), ip_address, _utcnow()))

    def list_audit_logs(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self._read(
            "SELECT id, operator, action, details_json, ip_address, created_at FROM audit_logs"
            " ORDER BY id DESC LIMIT ?", (limit,))

    # ----------------------------------------------------------------- health

    def health(self) -> Dict[str, Any]:
        try:
            start = time.monotonic()
            with self._connect() as conn:
                conn.execute("SELECT 1").fetchone()
            latency_ms = int((time.monotonic() - start) * 1000)
            size_bytes = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
            wal_bytes = 0
            wal_path = f"{self.db_path}-wal"
            if os.path.exists(wal_path):
                wal_bytes = os.path.getsize(wal_path)
            return {"ok": True, "latency_ms": latency_ms, "size_bytes": size_bytes,
                    "wal_size_bytes": wal_bytes, "path": self.db_path}
        except Exception as e:
            return {"ok": False, "error": str(e), "path": self.db_path}


# Process-wide singleton; gunicorn workers each hold their own, serialized by file locking.
_storage: Optional[DashboardStorage] = None
_storage_lock = threading.Lock()


def get_storage() -> DashboardStorage:
    global _storage
    with _storage_lock:
        if _storage is None:
            _storage = DashboardStorage()
            try:
                _storage.initialize()
            except Exception as e:
                get_logger().warning(f"Dashboard storage initialization failed, error: {e}")
        return _storage
