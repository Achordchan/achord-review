"""The dashboard deep-links "open in GitHub" to the exact review it published.

GithubProvider records the verdict-carrying review's html_url when it posts one,
and exposes it via the provider-neutral get_published_review_url(). These tests
use GithubProvider.__new__ to bypass network-bound __init__.
"""

from types import SimpleNamespace

from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.git_providers.github_provider import GithubProvider

REVIEW_URL = "https://github.com/owner/repo/pull/1#pullrequestreview-42"


class _FakePR:
    def __init__(self):
        self.create_review_calls = []

    def create_review(self, commit=None, comments=None, body=None, event=None):
        self.create_review_calls.append(
            {"commit": commit, "comments": comments, "body": body, "event": event})
        state = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED",
                 "COMMENT": "COMMENTED"}.get(event, "COMMENTED")
        return SimpleNamespace(id=42, state=state, html_url=REVIEW_URL)


def _make_provider():
    provider = GithubProvider.__new__(GithubProvider)
    provider.pr = _FakePR()
    provider.repo = "owner/repo"
    provider.pr_num = 1
    provider.max_comment_chars = 65000
    provider.last_commit_id = SimpleNamespace(sha="deadbeef")
    provider.diff_files = []
    provider.base_url = "https://api.github.com"
    return provider


def test_default_provider_reports_no_published_review_url():
    # GitProvider is abstract; exercise its default via the unbound method on a
    # bare object that never recorded a published review.
    assert GitProvider.get_published_review_url(SimpleNamespace()) is None


def test_submit_review_verdict_records_review_url():
    provider = _make_provider()
    assert provider.get_published_review_url() is None
    assert provider.submit_review_verdict("COMMENT", "body") is True
    assert provider.get_published_review_url() == REVIEW_URL


def test_inline_comments_with_verdict_record_review_url():
    provider = _make_provider()
    provider.publish_inline_comments([], review_body="summary", review_event="COMMENT")
    assert provider.get_published_review_url() == REVIEW_URL


def test_bare_inline_comment_batch_does_not_record_a_review_url():
    """A batch with no verdict/body is not the review the dashboard should link to."""
    provider = _make_provider()
    provider.publish_inline_comments([])
    assert provider.get_published_review_url() is None


def test_missing_html_url_does_not_break_publishing():
    provider = _make_provider()
    provider.pr.create_review = lambda **kwargs: SimpleNamespace(id=1, state="COMMENTED")
    assert provider.submit_review_verdict("COMMENT", "body") is True
    assert provider.get_published_review_url() is None
