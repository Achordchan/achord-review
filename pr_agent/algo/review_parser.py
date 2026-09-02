"""Narrow recovery helpers for structured PR review responses."""

from typing import Any

_REVIEW_FIELDS = frozenset({
    "ticket_compliance_check",
    "estimated_effort_to_review_[1-5]",
    "contribution_time_cost_estimate",
    "score",
    "relevant_tests",
    "insights_from_user_answers",
    "key_issues_to_review",
    "security_concerns",
    "todo_sections",
    "can_be_split",
})
_REVIEW_SEVERITIES = frozenset({"P0", "P1", "P2", "P3"})


def _is_positive_line_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return int(str(value).strip()) > 0
    except (TypeError, ValueError):
        return False


def _is_complete_finding(issue: Any, require_severity: bool) -> bool:
    if not isinstance(issue, dict):
        return False
    for field in ("relevant_file", "issue_header", "issue_content"):
        if not isinstance(issue.get(field), str) or not issue[field].strip():
            return False
    if not _is_positive_line_number(issue.get("start_line")):
        return False
    if not _is_positive_line_number(issue.get("end_line")):
        return False
    if int(str(issue["end_line"]).strip()) < int(str(issue["start_line"]).strip()):
        return False
    severity = str(issue.get("severity") or "").strip().upper()
    return not require_severity or severity in _REVIEW_SEVERITIES


def recover_missing_review_wrapper(data: Any, *, require_severity: bool = False) -> tuple[Any, bool]:
    """Recover a model response that omitted only the required top-level ``review`` key.

    A malformed clean response must never become an approval. Recovery therefore requires
    at least one complete finding, or an explicit security concern, and rejects unknown
    top-level fields. The original object is returned unchanged when these checks fail.
    """
    if not isinstance(data, dict) or "review" in data or not data:
        return data, False
    if not set(data).issubset(_REVIEW_FIELDS):
        return data, False

    issues = data.get("key_issues_to_review")
    if issues is not None and (not isinstance(issues, list)
                               or not all(_is_complete_finding(issue, require_severity) for issue in issues)):
        return data, False

    has_complete_finding = isinstance(issues, list) and bool(issues)
    security_concerns = data.get("security_concerns")
    has_explicit_security_concern = (
        isinstance(security_concerns, str)
        and bool(security_concerns.strip())
        and not security_concerns.strip().lower().startswith("no")
    )
    if not has_complete_finding and not has_explicit_security_concern:
        return data, False

    return {"review": data}, True
