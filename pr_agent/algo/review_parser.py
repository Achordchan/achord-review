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
_NO_SECURITY_PREFIXES = (
    "none",
    "n/a",
    "no security concern",
    "no security concerns",
    "no concerns",
    "no finding",
    "no findings",
    "no issues",
    "no vulnerabilities",
    "no vulnerability",
    "not detected",
    "not found",
    "not identified",
    "nothing found",
    "nothing identified",
    "there are no",
    "there is no",
)


def _is_positive_line_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return int(str(value).strip()) > 0
    except (TypeError, ValueError):
        return False


def _normalize_finding(issue: Any, require_severity: bool) -> dict | None:
    if not isinstance(issue, dict):
        return None
    for field in ("relevant_file", "issue_header", "issue_content"):
        if not isinstance(issue.get(field), str) or not issue[field].strip():
            return None
    if not _is_positive_line_number(issue.get("start_line")):
        return None
    end_line = issue["start_line"] if "end_line" not in issue else issue["end_line"]
    if not _is_positive_line_number(end_line):
        return None
    start_line_number = int(str(issue["start_line"]).strip())
    end_line_number = int(str(end_line).strip())
    if end_line_number < start_line_number:
        return None
    severity = str(issue.get("severity") or "").strip().upper()
    if severity and severity not in _REVIEW_SEVERITIES:
        return None
    if require_severity and not severity:
        return None
    normalized = dict(issue)
    if severity in _REVIEW_SEVERITIES:
        normalized["severity"] = severity
    normalized["start_line"] = start_line_number
    normalized["end_line"] = end_line_number
    return normalized


def _is_negative_security_summary(value: str) -> bool:
    normalized = " ".join(value.strip().lower().split())
    return normalized == "no" or normalized.startswith("no:") or any(
        normalized == prefix
        or normalized.startswith(f"{prefix} ")
        or normalized.startswith(f"{prefix}:")
        for prefix in _NO_SECURITY_PREFIXES
    )


def _is_explicit_security_concern(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = " ".join(value.strip().lower().split())
    if not normalized or _is_negative_security_summary(normalized):
        return False
    heading, separator, details = normalized.partition(":")
    return bool(
        separator
        and heading.strip()
        and details.strip()
        and not _is_negative_security_summary(details)
    )


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
    normalized_issues = None
    if issues is not None:
        if not isinstance(issues, list):
            return data, False
        normalized_issues = []
        for issue in issues:
            normalized_issue = _normalize_finding(issue, require_severity)
            if normalized_issue is None:
                return data, False
            normalized_issues.append(normalized_issue)

    has_complete_finding = isinstance(issues, list) and bool(issues)
    security_concerns = data.get("security_concerns")
    has_explicit_security_concern = _is_explicit_security_concern(security_concerns)
    if not has_complete_finding and not has_explicit_security_concern:
        return data, False

    normalized_data = dict(data)
    if normalized_issues is not None:
        normalized_data["key_issues_to_review"] = normalized_issues
    return {"review": normalized_data}, True
