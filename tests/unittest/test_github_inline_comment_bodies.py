from pr_agent.algo.inline_comment_dedup import can_verify_inline_comment_publication
from pr_agent.git_providers.git_provider import OwnVerdict
from pr_agent.git_providers.github_provider import GithubProvider


class _Comment:
    def __init__(self, body):
        self.body = body


class _PR:
    def __init__(self, comments):
        self._comments = comments
        self.calls = 0

    def get_comments(self):
        self.calls += 1
        return self._comments


def _provider(comments):
    provider = GithubProvider.__new__(GithubProvider)
    provider.pr = _PR(comments)
    return provider


class TestInlineCommentBodies:
    def test_returns_every_comment_body(self):
        provider = _provider([_Comment("first"), _Comment("second")])
        assert provider.get_inline_comment_bodies() == ["first", "second"]

    def test_missing_or_none_body_becomes_empty_string(self):
        provider = _provider([_Comment(None), object()])
        assert provider.get_inline_comment_bodies() == ["", ""]

    def test_no_comments_returns_empty_list(self):
        assert _provider([]).get_inline_comment_bodies() == []

    def test_recent_bodies_re_read_from_github(self):
        """Verification must not be served from a snapshot taken before publishing."""
        provider = _provider([_Comment("first")])
        provider.get_inline_comment_bodies()
        provider.get_recent_inline_comment_bodies()
        assert provider.pr.calls == 2

    def test_provider_now_satisfies_the_verification_gate(self):
        assert can_verify_inline_comment_publication(_provider([])) is True


class _Review:
    """A verdict review by default; pass marked=False for the inline-comment carrier."""

    def __init__(self, login, state, marked=True, sha="abc1234"):
        self.user = type("U", (), {"login": login})()
        self.state = state
        self.body = f"<!-- pr-agent-review-verdict: {sha} -->" if marked else ""


class _PRWithReviews(_PR):
    def __init__(self, reviews):
        super().__init__([])
        self._reviews = reviews

    def get_reviews(self):
        return self._reviews


class TestLatestOwnReviewState:
    @staticmethod
    def _provider(reviews, bot_user="achord-review[bot]"):
        from unittest.mock import patch
        provider = GithubProvider.__new__(GithubProvider)
        provider.pr = _PRWithReviews(reviews)
        patcher = patch("pr_agent.git_providers.github_provider.get_settings")
        mock = patcher.start()
        mock.return_value.get.side_effect = lambda key, default=None: (
            bot_user if key == "github_app.bot_user" else default)
        return provider, patcher

    def _state(self, reviews, bot_user="achord-review[bot]"):
        provider, patcher = self._provider(reviews, bot_user)
        try:
            return provider.get_latest_own_review_state()
        finally:
            patcher.stop()

    def test_no_reviews_returns_none(self):
        assert self._state([]) is None

    def test_other_reviewers_are_ignored(self):
        assert self._state([_Review("someone-else", "APPROVED")]) is None

    def test_latest_own_review_wins(self):
        reviews = [_Review("achord-review[bot]", "CHANGES_REQUESTED"),
                   _Review("achord-review[bot]", "APPROVED")]
        assert self._state(reviews) == "APPROVED"

    def test_pending_and_dismissed_states_are_skipped(self):
        reviews = [_Review("achord-review[bot]", "CHANGES_REQUESTED"),
                   _Review("achord-review[bot]", "DISMISSED"),
                   _Review("achord-review[bot]", "PENDING")]
        assert self._state(reviews) == "CHANGES_REQUESTED"

    def test_unconfigured_bot_user_disables_the_check(self):
        assert self._state([_Review("achord-review[bot]", "APPROVED")], bot_user="") is None

    def test_the_inline_comment_carrier_review_is_ignored(self):
        """Publishing inline comments creates an unmarked COMMENTED review of our own."""
        reviews = [_Review("achord-review[bot]", "CHANGES_REQUESTED"),
                   _Review("achord-review[bot]", "COMMENTED", marked=False)]
        assert self._state(reviews) == "CHANGES_REQUESTED"

    def test_only_carrier_reviews_means_no_standing_verdict(self):
        assert self._state([_Review("achord-review[bot]", "COMMENTED", marked=False)]) is None


class TestReviewedCommitIsRecovered:
    def _verdict(self, reviews):
        from unittest.mock import patch
        provider = GithubProvider.__new__(GithubProvider)
        provider.pr = _PRWithReviews(reviews)
        with patch("pr_agent.git_providers.github_provider.get_settings") as mock:
            mock.return_value.get.side_effect = lambda key, default=None: (
                "achord-review[bot]" if key == "github_app.bot_user" else default)
            return provider.get_latest_own_verdict()

    def test_state_and_sha_come_back_together(self):
        verdict = self._verdict([_Review("achord-review[bot]", "CHANGES_REQUESTED", sha="c0ffee1")])
        assert (verdict.state, verdict.sha) == ("CHANGES_REQUESTED", "c0ffee1")

    def test_marker_without_a_sha_is_still_a_verdict(self):
        review = _Review("achord-review[bot]", "APPROVED")
        review.body = "<!-- pr-agent-review-verdict -->"
        verdict = self._verdict([review])
        assert (verdict.state, verdict.sha) == ("APPROVED", None)

    def test_no_verdict_review_returns_an_empty_verdict(self):
        assert self._verdict([]) == OwnVerdict()

    def test_the_review_id_identifies_which_verdict_is_standing(self):
        """Two concurrent re-reviews of one commit share a sha; only the id separates them."""
        review = _Review("achord-review[bot]", "COMMENTED", sha="c0ffee1")
        review.id = 4242
        assert self._verdict([review]).review_id == 4242
