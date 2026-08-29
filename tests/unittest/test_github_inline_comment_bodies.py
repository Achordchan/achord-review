from pr_agent.algo.inline_comment_dedup import can_verify_inline_comment_publication
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
