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


def test_normalizes_start_line_only_finding_as_a_single_line_issue():
    finding = _finding(severity="p2")
    finding.pop("end_line")

    recovered, changed = recover_missing_review_wrapper(
        {"key_issues_to_review": [finding]}, require_severity=True)

    assert changed is True
    assert recovered["review"]["key_issues_to_review"][0]["start_line"] == 143
    assert recovered["review"]["key_issues_to_review"][0]["end_line"] == 143
    assert recovered["review"]["key_issues_to_review"][0]["severity"] == "P2"


def test_optional_severity_rejects_invalid_value_but_accepts_omission():
    invalid, invalid_changed = recover_missing_review_wrapper(
        {"key_issues_to_review": [_finding(severity="P9")]}, require_severity=False)
    without_severity = _finding()
    without_severity.pop("severity")
    recovered, changed = recover_missing_review_wrapper(
        {"key_issues_to_review": [without_severity]}, require_severity=False)

    assert invalid_changed is False
    assert invalid["key_issues_to_review"][0]["severity"] == "P9"
    assert changed is True
    assert "severity" not in recovered["review"]["key_issues_to_review"][0]


@pytest.mark.parametrize("data", [
    {"key_issues_to_review": []},
    {"security_concerns": "No"},
    {"key_issues_to_review": [_finding(severity="")]},
    {"key_issues_to_review": [_finding(start_line=0)]},
    {"key_issues_to_review": [_finding(start_line=144, end_line=143)]},
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


def test_security_heading_starting_with_no_letters_is_not_a_negative_answer():
    data = {
        "key_issues_to_review": [],
        "security_concerns": "Nonce validation bypass: attacker-controlled nonce is accepted",
    }

    recovered, changed = recover_missing_review_wrapper(data, require_severity=True)

    assert changed is True
    assert recovered == {"review": data}


def test_missing_authentication_heading_is_an_explicit_security_concern():
    data = {
        "key_issues_to_review": [],
        "security_concerns": "No authentication: the endpoint is publicly accessible",
    }

    recovered, changed = recover_missing_review_wrapper(data, require_severity=True)

    assert changed is True
    assert recovered == {"review": data}


@pytest.mark.parametrize("security_concerns", [
    "There are no security concerns",
    "N/A",
    "None",
    "No vulnerabilities: none found",
    "Vulnerabilities: none",
    "Security findings: nothing identified",
    "Possible concern without details",
])
def test_rejects_unwrapped_security_only_response_without_positive_concern(
        security_concerns):
    data = {"key_issues_to_review": [], "security_concerns": security_concerns}

    recovered, changed = recover_missing_review_wrapper(data, require_severity=True)

    assert changed is False
    assert recovered is data


def test_preserves_already_wrapped_review_response():
    data = {"review": {"key_issues_to_review": [_finding()]}}

    recovered, changed = recover_missing_review_wrapper(data, require_severity=True)

    assert changed is False
    assert recovered is data
