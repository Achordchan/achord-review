import pytest
from jinja2 import Environment, StrictUndefined

from pr_agent.algo.utils import format_severity_badge
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_reviewer import PRReviewer
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

VERDICT_KEYS = [
    "pr_reviewer.verdict_blocking_severities",
    "pr_reviewer.verdict_blocking_on_security_concerns",
    "pr_reviewer.enable_review_verdict",
    "config.publish_output",
]


@pytest.fixture(autouse=True)
def _restore_verdict_settings():
    snapshot = snapshot_settings(VERDICT_KEYS)
    yield
    restore_settings(snapshot)


def _issue(severity):
    return {"relevant_file": "a.py", "issue_header": "Possible Bug",
            "severity": severity, "issue_content": "...", "start_line": 1, "end_line": 2}


def _verdict(review):
    # _determine_review_verdict only reads its argument and the settings, never self
    return PRReviewer._determine_review_verdict(object(), {"review": review})


class TestDetermineReviewVerdict:
    @pytest.mark.parametrize("severities, expected", [
        ([], "APPROVE"),
        (["P3"], "COMMENT"),
        (["P2", "P3"], "COMMENT"),
        (["P1"], "REQUEST_CHANGES"),
        (["P0"], "REQUEST_CHANGES"),
        (["P3", "P0"], "REQUEST_CHANGES"),
    ])
    def test_severity_drives_the_event(self, severities, expected):
        review = {"security_concerns": "No",
                  "key_issues_to_review": [_issue(s) for s in severities]}
        assert _verdict(review)[0] == expected

    @pytest.mark.parametrize("security_concerns, expected", [
        ("No", "APPROVE"),
        ("no", "APPROVE"),
        ("", "APPROVE"),
        (None, "APPROVE"),
        ("Sensitive information exposure: the API key is logged", "REQUEST_CHANGES"),
    ])
    def test_security_concerns_block_regardless_of_severity(self, security_concerns, expected):
        review = {"security_concerns": security_concerns, "key_issues_to_review": []}
        assert _verdict(review)[0] == expected

    def test_security_blocking_can_be_disabled(self):
        get_settings().set("pr_reviewer.verdict_blocking_on_security_concerns", False)
        review = {"security_concerns": "SQL injection: ...", "key_issues_to_review": []}
        assert _verdict(review)[0] == "APPROVE"

    def test_blocking_severities_are_configurable(self):
        get_settings().set("pr_reviewer.verdict_blocking_severities", ["P0"])
        review = {"security_concerns": "No", "key_issues_to_review": [_issue("P1")]}
        assert _verdict(review)[0] == "COMMENT"

    @pytest.mark.parametrize("severity", ["p0", " P1 ", "P1"])
    def test_severity_matching_is_case_and_space_insensitive(self, severity):
        review = {"security_concerns": "No", "key_issues_to_review": [_issue(severity)]}
        assert _verdict(review)[0] == "REQUEST_CHANGES"

    @pytest.mark.parametrize("issues", [None, "No", {"not": "a list"}])
    def test_malformed_issue_lists_do_not_raise(self, issues):
        review = {"security_concerns": "No", "key_issues_to_review": issues}
        assert _verdict(review)[0] == "APPROVE"

    def test_issue_without_severity_is_non_blocking(self):
        review = {"security_concerns": "No",
                  "key_issues_to_review": [{"relevant_file": "a.py", "issue_header": "Nit"}]}
        assert _verdict(review)[0] == "COMMENT"

    def test_empty_review_approves(self):
        assert PRReviewer._determine_review_verdict(object(), {})[0] == "APPROVE"

    def test_reason_is_reported(self):
        review = {"security_concerns": "No", "key_issues_to_review": [_issue("P0")]}
        event, reason = _verdict(review)
        assert event == "REQUEST_CHANGES"
        assert "P0" in reason


class TestSeverityPrompt:
    """The severity field must appear only when the verdict feature is on."""

    @staticmethod
    def _render(require_severity):
        template = get_settings().pr_review_prompt.system
        variables = {
            "extra_instructions": "", "repo_context": "", "skills_context": "",
            "require_can_be_split_review": False, "related_tickets": "",
            "require_estimate_contribution_time_cost": False, "require_score": False,
            "require_tests": True, "question_str": "", "require_security_review": True,
            "require_todo_scan": False, "require_estimate_effort_to_review": True,
            "num_max_findings": 3, "num_pr_files": 1, "is_ai_metadata": False,
            "require_severity": require_severity,
        }
        return Environment(undefined=StrictUndefined).from_string(template).render(variables)

    def test_severity_absent_when_disabled(self):
        # "severity" also appears in the prompt's prose, so assert on the schema field itself
        rendered = self._render(False)
        assert "severity: str = Field" not in rendered
        assert "severity: |" not in rendered

    def test_severity_present_when_enabled(self):
        rendered = self._render(True)
        assert "severity: str = Field" in rendered
        for level in ("P0", "P1", "P2", "P3"):
            assert level in rendered


class TestSubmitVerdictGuards:
    """A verdict must never be invented from missing data."""

    class _Provider:
        def __init__(self):
            self.calls = []

        def submit_review_verdict(self, event, body=""):
            self.calls.append((event, body))
            return True

    def _reviewer(self, review_data):
        reviewer = PRReviewer.__new__(PRReviewer)
        reviewer.review_data = review_data
        reviewer.git_provider = self._Provider()
        return reviewer

    @pytest.mark.parametrize("review_data", [None, {}])
    def test_unparsed_review_submits_no_verdict(self, review_data):
        get_settings().set("pr_reviewer.enable_review_verdict", True)
        get_settings().set("config.publish_output", True)
        reviewer = self._reviewer(review_data)
        reviewer._submit_review_verdict()
        assert reviewer.git_provider.calls == []

    def test_disabled_by_default_submits_no_verdict(self):
        get_settings().set("pr_reviewer.enable_review_verdict", False)
        get_settings().set("config.publish_output", True)
        reviewer = self._reviewer({"review": {"security_concerns": "No", "key_issues_to_review": []}})
        reviewer._submit_review_verdict()
        assert reviewer.git_provider.calls == []

    def test_parsed_review_submits_the_verdict(self):
        get_settings().set("pr_reviewer.enable_review_verdict", True)
        get_settings().set("config.publish_output", True)
        reviewer = self._reviewer({"review": {"security_concerns": "No", "key_issues_to_review": []}})
        reviewer._submit_review_verdict()
        assert len(reviewer.git_provider.calls) == 1
        assert reviewer.git_provider.calls[0][0] == "APPROVE"


class TestSeverityBadge:
    """Severity is surfaced as a visible badge, and absent severities render nothing."""

    @pytest.mark.parametrize("severity, icon", [
        ("P0", "\U0001F534"), ("P1", "\U0001F7E0"),
        ("P2", "\U0001F7E1"), ("P3", "\U0001F535"),
    ])
    def test_known_levels_render_icon_and_label(self, severity, icon):
        badge = format_severity_badge(severity)
        assert icon in badge
        assert severity in badge

    @pytest.mark.parametrize("severity", ["p1", " P1 "])
    def test_matching_is_case_and_space_insensitive(self, severity):
        assert "P1" in format_severity_badge(severity)

    @pytest.mark.parametrize("severity", [None, "", "P9", "critical", 3, {}])
    def test_unknown_severity_renders_nothing(self, severity):
        assert format_severity_badge(severity) == ""

    def test_plain_markdown_avoids_html_entities(self):
        badge = format_severity_badge("P1", gfm_supported=False)
        assert "&nbsp;" not in badge
        assert "`P1`" in badge

    def test_badge_ends_with_a_separator_so_titles_do_not_collide(self):
        assert format_severity_badge("P0").endswith("&nbsp;")
        assert format_severity_badge("P0", gfm_supported=False).endswith(" ")
