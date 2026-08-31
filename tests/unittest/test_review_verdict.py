import pytest
from jinja2 import Environment, StrictUndefined

from pr_agent.algo.utils import CLEAN_REVIEW_MESSAGES, clean_review_message, format_severity_badge
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.tools.pr_reviewer import VERDICT_REASON_PREFIX, PRReviewer
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

VERDICT_KEYS = [
    "pr_reviewer.verdict_blocking_severities",
    "pr_reviewer.verdict_blocking_on_security_concerns",
    "pr_reviewer.enable_review_verdict",
    "config.publish_output",
    "config.is_auto_command",
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

        def get_latest_own_review_state(self):
            return None

        def submit_review_verdict(self, event, body=""):
            self.calls.append((event, body))
            return True

    def _reviewer(self, review_data):
        reviewer = PRReviewer.__new__(PRReviewer)
        reviewer.pr_url = "https://api.github.com/repos/o/r/pulls/1"
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

    @pytest.mark.parametrize("severity, color", [
        ("P0", "red"), ("P1", "orange"), ("P2", "yellow"), ("P3", "blue"),
    ])
    def test_known_levels_render_a_coloured_chip(self, severity, color):
        badge = format_severity_badge(severity)
        assert f"{severity}-{color}" in badge
        assert badge.startswith("<sub>![")

    @pytest.mark.parametrize("severity, icon", [
        ("P0", "\U0001F534"), ("P1", "\U0001F7E0"),
        ("P2", "\U0001F7E1"), ("P3", "\U0001F535"),
    ])
    def test_plain_markdown_falls_back_to_an_emoji(self, severity, icon):
        badge = format_severity_badge(severity, gfm_supported=False)
        assert icon in badge
        assert severity in badge

    @pytest.mark.parametrize("severity", ["p1", " P1 "])
    def test_matching_is_case_and_space_insensitive(self, severity):
        assert "P1" in format_severity_badge(severity)

    @pytest.mark.parametrize("severity", [None, "", "P9", "critical", 3, {}])
    def test_unknown_severity_renders_nothing(self, severity):
        assert format_severity_badge(severity) == ""

    def test_plain_markdown_avoids_html_and_images(self):
        badge = format_severity_badge("P1", gfm_supported=False)
        assert "&nbsp;" not in badge
        assert "img.shields.io" not in badge
        assert "`P1`" in badge

    def test_badge_ends_with_a_separator_so_titles_do_not_collide(self):
        assert format_severity_badge("P0").endswith("&nbsp;")
        assert format_severity_badge("P0", gfm_supported=False).endswith(" ")


class TestVerdictIsNotRestated:
    """A verdict is submitted only when the standing review state actually changes."""

    class _Provider:
        def __init__(self, current_state):
            self.current_state = current_state
            self.calls = []

        def get_latest_own_review_state(self):
            return self.current_state

        def submit_review_verdict(self, event, body=""):
            self.calls.append(event)
            return True

    def _run(self, current_state, review):
        get_settings().set("pr_reviewer.enable_review_verdict", True)
        get_settings().set("config.publish_output", True)
        get_settings().set("config.is_auto_command", True)
        reviewer = PRReviewer.__new__(PRReviewer)
        reviewer.pr_url = "https://api.github.com/repos/o/r/pulls/1"
        reviewer.review_data = {"review": review}
        reviewer.git_provider = self._Provider(current_state)
        reviewer._submit_review_verdict()
        return reviewer.git_provider.calls

    CLEAN = {"security_concerns": "No", "key_issues_to_review": []}
    BLOCKING = {"security_concerns": "No",
                "key_issues_to_review": [{"severity": "P1", "issue_header": "x"}]}

    @pytest.mark.parametrize("current_state, review", [
        ("APPROVED", CLEAN),
        ("CHANGES_REQUESTED", BLOCKING),
    ])
    def test_unchanged_verdict_is_not_resubmitted(self, current_state, review):
        assert self._run(current_state, review) == []

    @pytest.mark.parametrize("current_state, review, expected", [
        (None, CLEAN, "APPROVE"),                       # first review on the PR
        ("CHANGES_REQUESTED", CLEAN, "APPROVE"),        # issues were fixed
        ("APPROVED", BLOCKING, "REQUEST_CHANGES"),      # a regression was pushed
        ("COMMENTED", BLOCKING, "REQUEST_CHANGES"),
    ])
    def test_changed_verdict_is_submitted(self, current_state, review, expected):
        assert self._run(current_state, review) == [expected]


class TestSingleReviewSubmission:
    """Summary, findings and verdict go out as one review, or not at all."""

    class _Provider:
        def __init__(self, current_state=None, publish_ok=True, reviewed_sha=None,
                     head_sha="deadbeefcafe"):
            self.current_state = current_state
            self.publish_ok = publish_ok
            self.reviewed_sha = reviewed_sha
            self.head_sha = head_sha
            self.suggestion_calls = []
            self.verdict_calls = []

        def get_latest_own_verdict(self):
            return self.current_state, self.reviewed_sha

        def get_head_commit_sha(self):
            return self.head_sha

        def get_latest_own_review_state(self):
            return self.current_state

        def mark_review_verdict_body(self, body):
            return f"{body}\n\n<!-- marked -->"

        def publish_code_suggestions(self, comments, review_body=None, review_event=None):
            self.suggestion_calls.append((comments, review_body, review_event))
            return self.publish_ok

        def submit_review_verdict(self, event, body=""):
            self.verdict_calls.append((event, body))
            return True

    CLEAN = {"security_concerns": "No", "key_issues_to_review": []}
    BLOCKING = {"security_concerns": "No",
                "key_issues_to_review": [{"severity": "P1", "issue_header": "x"}]}

    def _reviewer(self, provider, review, comments):
        get_settings().set("config.is_auto_command", True)
        reviewer = PRReviewer.__new__(PRReviewer)
        reviewer.pr_url = "https://api.github.com/repos/o/r/pulls/1"
        reviewer.review_data = {"review": review}
        reviewer.deferred_review_comments = comments
        reviewer.git_provider = provider
        return reviewer

    def test_findings_and_verdict_go_out_together(self):
        provider = self._Provider()
        reviewer = self._reviewer(provider, self.BLOCKING, [{"body": "finding"}])
        reviewer._publish_single_review("SUMMARY")
        assert len(provider.suggestion_calls) == 1
        comments, body, event = provider.suggestion_calls[0]
        assert comments == [{"body": "finding"}]
        assert body.startswith("SUMMARY")
        assert event == "REQUEST_CHANGES"
        # a second, separate verdict review would be a second notification
        assert provider.verdict_calls == []

    def test_verdict_only_when_there_are_no_inline_findings(self):
        provider = self._Provider()
        reviewer = self._reviewer(provider, self.CLEAN, [])
        reviewer._publish_single_review("SUMMARY")
        assert provider.suggestion_calls == []
        assert len(provider.verdict_calls) == 1
        assert provider.verdict_calls[0][0] == "APPROVE"

    def test_nothing_new_and_unchanged_verdict_stays_silent(self):
        provider = self._Provider(current_state="CHANGES_REQUESTED")
        reviewer = self._reviewer(provider, self.BLOCKING, [])
        reviewer._publish_single_review("SUMMARY")
        assert provider.suggestion_calls == []
        assert provider.verdict_calls == []

    def test_new_findings_are_posted_even_when_the_verdict_is_unchanged(self):
        provider = self._Provider(current_state="CHANGES_REQUESTED")
        reviewer = self._reviewer(provider, self.BLOCKING, [{"body": "new finding"}])
        reviewer._publish_single_review("SUMMARY")
        assert len(provider.suggestion_calls) == 1

    def test_failed_submission_falls_back_to_a_verdict_review(self):
        provider = self._Provider(publish_ok=False)
        reviewer = self._reviewer(provider, self.BLOCKING, [{"body": "finding"}])
        reviewer._publish_single_review("SUMMARY")
        assert len(provider.suggestion_calls) == 1
        assert len(provider.verdict_calls) == 1, "the review must still reach the PR"


class TestVerdictMarkerTravelsWithTheVerdict:
    """Whichever review carries the verdict must be recognisable on the next run."""

    def test_single_review_body_is_marked(self):
        from pr_agent.git_providers.github_provider import VERDICT_MARKER

        provider = TestSingleReviewSubmission._Provider()
        provider.mark_review_verdict_body = (
            lambda body: GithubProvider.mark_review_verdict_body(provider, body))
        reviewer = PRReviewer.__new__(PRReviewer)
        reviewer.pr_url = "https://api.github.com/repos/o/r/pulls/1"
        reviewer.review_data = {"review": TestSingleReviewSubmission.BLOCKING}
        reviewer.deferred_review_comments = [{"body": "finding"}]
        reviewer.git_provider = provider
        reviewer._publish_single_review("SUMMARY")
        _, body, _ = provider.suggestion_calls[0]
        assert VERDICT_MARKER in body

    def test_marking_is_idempotent(self):
        from pr_agent.git_providers.github_provider import VERDICT_MARKER

        provider = GithubProvider.__new__(GithubProvider)
        once = provider.mark_review_verdict_body("body")
        assert provider.mark_review_verdict_body(once) == once
        assert once.count(VERDICT_MARKER) == 1

    def test_marking_an_empty_body_still_produces_a_marker(self):
        from pr_agent.git_providers.github_provider import VERDICT_MARKER_RE

        provider = GithubProvider.__new__(GithubProvider)
        assert VERDICT_MARKER_RE.search(provider.mark_review_verdict_body(""))

    def test_marker_records_the_reviewed_commit(self):
        from pr_agent.git_providers.github_provider import VERDICT_MARKER_RE

        provider = GithubProvider.__new__(GithubProvider)
        provider.last_commit_id = type("C", (), {"sha": "abc1234def5678"})()
        match = VERDICT_MARKER_RE.search(provider.mark_review_verdict_body("body"))
        assert match.group(1) == "abc1234def5678"


class TestAlreadyReviewedCommit:
    """The same commit is never reviewed twice, however the model rewords itself."""

    def _run(self, reviewed_sha, head_sha, comments):
        get_settings().set("config.is_auto_command", True)
        provider = TestSingleReviewSubmission._Provider(
            current_state="CHANGES_REQUESTED", reviewed_sha=reviewed_sha, head_sha=head_sha)
        reviewer = PRReviewer.__new__(PRReviewer)
        reviewer.pr_url = "https://api.github.com/repos/o/r/pulls/1"
        reviewer.review_data = {"review": TestSingleReviewSubmission.BLOCKING}
        reviewer.deferred_review_comments = comments
        reviewer.git_provider = provider
        reviewer._publish_single_review("SUMMARY")
        return provider

    def test_same_commit_stays_silent_even_with_findings(self):
        provider = self._run("abc123def456", "abc123def456", [{"body": "reworded finding"}])
        assert provider.suggestion_calls == []
        assert provider.verdict_calls == []

    def test_new_commit_is_reviewed(self):
        provider = self._run("abc123def456", "999999999999", [{"body": "finding"}])
        assert len(provider.suggestion_calls) == 1

    def test_missing_sha_falls_back_to_the_verdict_check(self):
        """An older review without a recorded commit must not block all future reviews."""
        provider = self._run(None, "abc123def456", [{"body": "finding"}])
        assert len(provider.suggestion_calls) == 1


class TestExplicitRequestIsAlwaysAnswered:
    """A review someone asked for is never swallowed by the repeat guards."""

    def _run(self, review, comments, current_state, reviewed_sha, head_sha):
        get_settings().set("config.is_auto_command", False)
        provider = TestSingleReviewSubmission._Provider(
            current_state=current_state, reviewed_sha=reviewed_sha, head_sha=head_sha)
        reviewer = PRReviewer.__new__(PRReviewer)
        reviewer.pr_url = "https://api.github.com/repos/o/r/pulls/1"
        reviewer.review_data = {"review": review}
        reviewer.deferred_review_comments = comments
        reviewer.git_provider = provider
        reviewer._publish_single_review("SUMMARY")
        return provider

    def test_same_commit_is_reviewed_again_on_request(self):
        """@mention after a fix must answer, not look broken."""
        provider = self._run(TestSingleReviewSubmission.BLOCKING, [{"body": "finding"}],
                             "CHANGES_REQUESTED", "abc123def456", "abc123def456")
        assert len(provider.suggestion_calls) == 1

    def test_unchanged_verdict_with_no_findings_still_answers(self):
        provider = self._run(TestSingleReviewSubmission.CLEAN, [], "APPROVED", "abc123", "abc123")
        assert [event for event, _ in provider.verdict_calls] == ["APPROVE"]

    def test_automatic_trigger_still_stays_silent(self):
        """The guards must keep working for the webhook path they were written for."""
        get_settings().set("config.is_auto_command", True)
        provider = TestSingleReviewSubmission._Provider(
            current_state="CHANGES_REQUESTED", reviewed_sha="abc123", head_sha="abc123")
        reviewer = PRReviewer.__new__(PRReviewer)
        reviewer.pr_url = "https://api.github.com/repos/o/r/pulls/1"
        reviewer.review_data = {"review": TestSingleReviewSubmission.BLOCKING}
        reviewer.deferred_review_comments = [{"body": "finding"}]
        reviewer.git_provider = provider
        reviewer._publish_single_review("SUMMARY")
        assert provider.suggestion_calls == []
        assert provider.verdict_calls == []

    def test_standalone_verdict_is_resubmitted_on_request(self):
        get_settings().set("pr_reviewer.enable_review_verdict", True)
        get_settings().set("config.publish_output", True)
        get_settings().set("config.is_auto_command", False)
        reviewer = PRReviewer.__new__(PRReviewer)
        reviewer.pr_url = "https://api.github.com/repos/o/r/pulls/1"
        reviewer.review_data = {"review": TestVerdictIsNotRestated.CLEAN}
        reviewer.git_provider = TestVerdictIsNotRestated._Provider("APPROVED")
        reviewer._submit_review_verdict()
        assert reviewer.git_provider.calls == ["APPROVE"]


class TestCleanReviewSignOff:
    """A review that found nothing says so in words, not as an empty verdict."""

    def _body(self, review, comments, head_sha="deadbeefcafe"):
        get_settings().set("config.is_auto_command", True)
        provider = TestSingleReviewSubmission._Provider(head_sha=head_sha)
        reviewer = PRReviewer.__new__(PRReviewer)
        reviewer.pr_url = "https://api.github.com/repos/o/r/pulls/1"
        reviewer.review_data = {"review": review}
        reviewer.deferred_review_comments = comments
        reviewer.git_provider = provider
        reviewer._publish_single_review("SUMMARY")
        if comments:
            return provider.suggestion_calls[0][1]
        return provider.verdict_calls[0][1]

    def test_clean_review_signs_off_warmly(self):
        body = self._body(TestSingleReviewSubmission.CLEAN, [])
        assert any(message in body for message in CLEAN_REVIEW_MESSAGES)
        assert VERDICT_REASON_PREFIX not in body

    def test_a_review_with_findings_keeps_the_verdict_line(self):
        body = self._body(TestSingleReviewSubmission.BLOCKING, [{"body": "finding"}])
        assert VERDICT_REASON_PREFIX in body
        assert not any(message in body for message in CLEAN_REVIEW_MESSAGES)

    def test_wording_is_stable_for_one_commit(self):
        """A sign-off that rephrases itself on every run reads as chatter."""
        assert clean_review_message("abc123") == clean_review_message("abc123")

    def test_wording_varies_across_commits(self):
        seen = {clean_review_message(f"commit{i}") for i in range(60)}
        assert len(seen) > 1
        assert seen <= set(CLEAN_REVIEW_MESSAGES)

    def test_every_preset_is_reachable(self):
        seen = {clean_review_message(f"commit{i}") for i in range(4000)}
        assert seen == set(CLEAN_REVIEW_MESSAGES)


class TestConcurrentRunOnTheSameCommit:
    """A push trigger and a mention seconds apart must not answer the same commit twice."""

    def _run(self, is_auto, verdict_sha_at_start, reviewed_sha, head_sha="cbdb45ab8b91"):
        get_settings().set("config.is_auto_command", is_auto)
        provider = TestSingleReviewSubmission._Provider(
            current_state="CHANGES_REQUESTED", reviewed_sha=reviewed_sha, head_sha=head_sha)
        reviewer = PRReviewer.__new__(PRReviewer)
        reviewer.pr_url = "https://api.github.com/repos/o/r/pulls/1"
        reviewer.review_data = {"review": TestSingleReviewSubmission.BLOCKING}
        reviewer.deferred_review_comments = [{"body": "finding"}]
        reviewer.git_provider = provider
        reviewer._publish_single_review("SUMMARY", verdict_sha_at_start)
        return provider

    def _silent(self, provider):
        return provider.suggestion_calls == [] and provider.verdict_calls == []

    @pytest.mark.parametrize("is_auto", [True, False])
    def test_a_verdict_posted_mid_run_silences_this_one(self, is_auto):
        """The other run reviewed our head commit while we were thinking - it answered."""
        provider = self._run(is_auto, verdict_sha_at_start="46abe5425a92", reviewed_sha="cbdb45ab8b91")
        assert self._silent(provider), "the same commit must not be reviewed twice"

    def test_a_first_ever_review_is_still_silenced_by_a_concurrent_one(self):
        provider = self._run(False, verdict_sha_at_start=None, reviewed_sha="cbdb45ab8b91")
        assert self._silent(provider)

    def test_a_repeat_request_on_an_already_reviewed_commit_is_still_answered(self):
        """The verdict was standing before we started, so nobody has answered this request."""
        provider = self._run(False, verdict_sha_at_start="cbdb45ab8b91", reviewed_sha="cbdb45ab8b91")
        assert len(provider.suggestion_calls) == 1

    def test_an_unread_snapshot_does_not_silence_a_requested_review(self):
        """Failing to read the standing verdict must not turn into silence."""
        from pr_agent.tools.pr_reviewer import _VERDICT_SNAPSHOT_UNSET

        provider = self._run(False, verdict_sha_at_start=_VERDICT_SNAPSHOT_UNSET,
                             reviewed_sha="cbdb45ab8b91")
        assert len(provider.suggestion_calls) == 1

    def test_a_new_commit_is_reviewed_normally(self):
        provider = self._run(True, verdict_sha_at_start="46abe5425a92", reviewed_sha="46abe5425a92")
        assert len(provider.suggestion_calls) == 1

    def test_snapshot_failure_disables_the_guard_rather_than_the_review(self):
        from pr_agent.tools.pr_reviewer import _VERDICT_SNAPSHOT_UNSET

        class _Broken:
            def get_latest_own_verdict(self):
                raise RuntimeError("GitHub is down")

        reviewer = PRReviewer.__new__(PRReviewer)
        reviewer.pr_url = "https://api.github.com/repos/o/r/pulls/1"
        reviewer.git_provider = _Broken()
        assert reviewer._standing_verdict_sha() is _VERDICT_SNAPSHOT_UNSET


class TestConcurrentApprovalIsNotDoubled:
    """The verdict-only path (no inline findings) must be guarded too.

    Observed on PR 27: push 0a7ad8c and a mention 19s later both approved it, and the
    second approval was posted 87s after the first because the manual run is exempt from
    the already-reviewed guard.
    """

    def _run(self, is_auto, verdict_sha_at_start, reviewed_sha, head_sha="0a7ad8cd1234"):
        get_settings().set("config.is_auto_command", is_auto)
        provider = TestSingleReviewSubmission._Provider(
            current_state="COMMENTED", reviewed_sha=reviewed_sha, head_sha=head_sha)
        reviewer = PRReviewer.__new__(PRReviewer)
        reviewer.pr_url = "https://api.github.com/repos/o/r/pulls/1"
        reviewer.review_data = {"review": TestSingleReviewSubmission.CLEAN}
        reviewer.deferred_review_comments = []
        reviewer.git_provider = provider
        reviewer._publish_single_review("SUMMARY", verdict_sha_at_start)
        return provider

    def test_the_run_that_finishes_first_approves(self):
        """Nothing landed while it was thinking, so the standing verdict is still the old commit."""
        provider = self._run(True, verdict_sha_at_start="7100c7d89999", reviewed_sha="7100c7d89999")
        assert [event for event, _ in provider.verdict_calls] == ["APPROVE"]

    @pytest.mark.parametrize("is_auto", [True, False])
    def test_the_run_that_finishes_second_stays_silent(self, is_auto):
        provider = self._run(is_auto, verdict_sha_at_start="7100c7d89999", reviewed_sha="0a7ad8cd1234")
        assert provider.verdict_calls == [], "one commit must not be approved twice"
        assert provider.suggestion_calls == []
