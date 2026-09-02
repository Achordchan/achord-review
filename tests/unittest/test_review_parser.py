import pytest

from pr_agent.algo.review_parser import recover_missing_review_wrapper


def _finding(**overrides):
    finding = {
        "relevant_file": "pr_agent/dashboard/ops.py",
        "issue_header": "Enforce the probe timeout",
        "severity": "P2",
        "issue_content": "The configured timeout is accepted but not enforced.",
        "start_line": 143,
        "end_line": 143,
    }
    finding.update(overrides)
    return finding


def test_recovers_complete_findings_when_only_review_wrapper_is_missing():
    data = {
        "key_issues_to_review": [_finding()],
        "security_concerns": "No",
    }

    recovered, changed = recover_missing_review_wrapper(data, require_severity=True)

    assert changed is True
    assert recovered == {"review": data}


@pytest.mark.parametrize("data", [
    {"key_issues_to_review": []},
    {"security_concerns": "No"},
    {"key_issues_to_review": [_finding(severity="")]},
    {"key_issues_to_review": [_finding(start_line=0)]},
    {"key_issues_to_review": [_finding()], "unexpected": "value"},
])
def test_rejects_incomplete_or_ambiguous_unwrapped_responses(data):
    recovered, changed = recover_missing_review_wrapper(data, require_severity=True)

    assert changed is False
    assert recovered is data


def test_recovers_explicit_security_concern_without_findings():
    data = {
        "key_issues_to_review": [],
        "security_concerns": "Authentication bypass: missing authorization check",
    }

    recovered, changed = recover_missing_review_wrapper(data, require_severity=True)

    assert changed is True
    assert recovered == {"review": data}


def test_preserves_already_wrapped_review_response():
    data = {"review": {"key_issues_to_review": [_finding()]}}

    recovered, changed = recover_missing_review_wrapper(data, require_severity=True)

    assert changed is False
    assert recovered is data
