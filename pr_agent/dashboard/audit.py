"""Audit hook: persist every PR review run into the dashboard storage.

Called from PRReviewer.run() at the start and on every terminal path. Every
function here is fail-safe: any storage error is logged and swallowed, the
review flow itself is never disturbed. Terminal writes run off the event loop
via asyncio.to_thread and are awaited before the review request returns, so a
graceful worker shutdown cannot leave a completed run permanently RUNNING.
"""

import asyncio
import re
from typing import Optional

from pr_agent.algo.run_details import get_run_details
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

_PR_URL_RE = re.compile(r"(?:pull|pull-requests|merge_requests)/(\d+)")
_VALID_SEVERITIES = {"P0", "P1", "P2", "P3"}
_MAX_SQLITE_INTEGER = 2 ** 63 - 1


def _parse_pr_url(pr_url: str) -> tuple:
    """Extract (repo_full_name, pr_number) from a provider PR URL, best effort."""
    number = None
    match = _PR_URL_RE.search(pr_url or "")
    if match:
        number = int(match.group(1))
    repo = ""
    try:
        # https://github.com/<owner>/<repo>/pull/13
        parts = pr_url.split("://", 1)[-1].split("/", 1)[1].rstrip("/").split("/")
        if len(parts) >= 2:
            repo = f"{parts[0]}/{parts[1]}"
    except Exception as e:  # noqa: BLE001 — any odd URL shape falls back to ("", 0)
        get_logger().debug(f"Dashboard audit could not parse PR URL, error: {e}")
    return repo, number or 0


def _run_payload_fields() -> dict:
    details = get_run_details()
    fields = {"model": "", "reasoning_effort": "", "prompt_tokens": 0,
              "completion_tokens": 0, "total_tokens": 0, "duration_ms": 0}
    if details is not None:
        fields.update({
            "model": details.model_used or str(get_settings().get("config.model", "") or ""),
            "reasoning_effort": str(get_settings().get("config.reasoning_effort", "") or ""),
            "prompt_tokens": details.prompt_tokens,
            "completion_tokens": details.completion_tokens,
            "total_tokens": details.total_tokens,
            "duration_ms": int(details.duration_seconds * 1000),
        })
    return fields


def _run_audit() -> Optional:
    from pr_agent.dashboard.storage import get_storage
    try:
        return get_storage()
    except Exception as e:
        get_logger().warning(f"Dashboard audit storage unavailable, error: {e}")
        return None


def review_started(pr_url: str, sender: str = "", trigger_type: str = "manual",
                   command: str = "/review", commit_sha: str = "", pr_title: str = "",
                   repo_name: str = "", pr_number: int = 0, request_id: str = "") -> str:
    """Insert a RUNNING record; returns the request_id ("" when disabled or failed)."""
    try:
        if not get_settings().get("config.dashboard_audit_enabled", True):
            return ""
        storage = _run_audit()
        if storage is None:
            return ""
        parsed_repo, parsed_number = _parse_pr_url(pr_url)
        repo = repo_name or parsed_repo
        number = pr_number or parsed_number
        request_id = storage.create_review(
            repo_name=repo, pr_number=number, pr_url=pr_url, command=command,
            pr_title=pr_title, sender=sender, trigger_type=trigger_type,
            commit_sha=commit_sha, request_id=request_id)
        return request_id or ""
    except Exception as e:
        get_logger().warning(f"Dashboard audit (review_started) failed, error: {e}")
        return ""


async def review_finished(request_id: str, verdict: str = "", verdict_reason: str = "",
                          markdown_output: str = "", raw_prediction: str = "",
                          issues: Optional[list] = None) -> None:
    """Complete the record with usage, findings and verdict in one transaction."""
    if not request_id:
        return

    def _work():
        try:
            storage = _run_audit()
            if storage is None:
                return
            fields = _run_payload_fields()
            clean_issues = []
            for issue in issues or []:
                if not isinstance(issue, dict):
                    continue
                raw_severity = str(issue.get("severity") or "").strip()
                normalized_severity = raw_severity.upper()
                clean_issues.append({
                    "severity": (
                        normalized_severity
                        if normalized_severity in _VALID_SEVERITIES
                        else raw_severity
                    ),
                    "relevant_file": str(issue.get("relevant_file") or "").strip(),
                    "relevant_lines_start": _as_int(issue.get("start_line")),
                    "relevant_lines_end": _as_int(issue.get("end_line")),
                    "issue_summary": str(issue.get("issue_header") or "").strip(),
                    "suggestion": str(issue.get("issue_content") or "").strip(),
                })
            # single transaction: usage, findings and the COMPLETED status land
            # together, so a status=COMPLETED read never races a missing finding
            storage.finish_review(
                request_id, clean_issues, verdict=verdict, verdict_reason=verdict_reason,
                markdown_output=markdown_output, raw_prediction=raw_prediction, **fields)
        except Exception as e:
            get_logger().warning(f"Dashboard audit (review_finished) failed, error: {e}")

    await asyncio.to_thread(_work)


async def review_failed(request_id: str, error_message: str) -> None:
    if not request_id:
        return

    def _work():
        try:
            storage = _run_audit()
            if storage is None:
                return
            fields = _run_payload_fields()
            storage.fail_review(request_id, error_message[:2000], **fields)
        except Exception as e:
            get_logger().warning(f"Dashboard audit (review_failed) failed, error: {e}")

    await asyncio.to_thread(_work)


async def review_skipped(request_id: str, reason: str) -> None:
    """Close a RUNNING record for a run that exited before publishing.

    Without this the record would stay RUNNING forever and inflate the
    dashboard's active-review count.
    """
    if not request_id:
        return

    def _work():
        try:
            storage = _run_audit()
            if storage is None:
                return
            fields = _run_payload_fields()
            storage.skip_review(request_id, reason[:2000], **fields)
        except Exception as e:
            get_logger().warning(f"Dashboard audit (review_skipped) failed, error: {e}")

    await asyncio.to_thread(_work)


async def review_heartbeat(request_id: str) -> None:
    """Refresh one RUNNING record without affecting the review flow on failure."""
    if not request_id:
        return

    def _work():
        try:
            storage = _run_audit()
            if storage is not None:
                storage.touch_review(request_id)
        except Exception as e:
            get_logger().warning(f"Dashboard audit (review_heartbeat) failed, error: {e}")

    await asyncio.to_thread(_work)


async def review_heartbeat_loop(request_id: str, interval_seconds: Optional[float] = None) -> None:
    """Keep a long-running review live until its owner cancels this task."""
    if not request_id:
        return
    if interval_seconds is None:
        from pr_agent.dashboard.storage import REVIEW_HEARTBEAT_SECONDS
        interval_seconds = REVIEW_HEARTBEAT_SECONDS
    while True:
        await asyncio.sleep(interval_seconds)
        await review_heartbeat(request_id)


def _as_int(value) -> Optional[int]:
    try:
        number = int(str(value).strip())
        return number if 1 <= number <= _MAX_SQLITE_INTEGER else None
    except (TypeError, ValueError):
        return None
