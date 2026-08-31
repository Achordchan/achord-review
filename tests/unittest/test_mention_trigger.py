import pytest

from pr_agent.config_loader import get_settings
from pr_agent.servers.github_app import normalize_mention_command
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

MENTION_KEYS = ["github_app.mention_trigger", "github_app.mention_default_command"]


@pytest.fixture(autouse=True)
def _restore_mention_settings():
    snapshot = snapshot_settings(MENTION_KEYS)
    yield
    restore_settings(snapshot)


@pytest.fixture
def mention_enabled():
    get_settings().set("github_app.mention_trigger", "@achord-review")
    get_settings().set("github_app.mention_default_command", "/review")


class TestNormalizeMentionCommand:
    @pytest.mark.parametrize("comment_body", [
        "@achord-review review",
        "/review",
        "",
        None,
        "just a normal comment",
    ])
    def test_disabled_by_default_leaves_body_untouched(self, comment_body):
        get_settings().set("github_app.mention_trigger", "")
        assert normalize_mention_command(comment_body) == comment_body

    @pytest.mark.parametrize("comment_body, expected", [
        ("@achord-review review", "/review"),
        ("@achord-review  review", "/review"),
        ("@achord-review /review", "/review"),
        ("@achord-review", "/review"),                      # bare mention -> default command
        ("@achord-review   ", "/review"),
        ("@achord-review ask why is this needed?", "/ask why is this needed?"),
        ("hey @achord-review review please", "/review please"),
        ("@achord-review review\nsome trailing text", "/review"),
    ])
    def test_mention_is_translated_to_a_command(self, mention_enabled, comment_body, expected):
        assert normalize_mention_command(comment_body) == expected

    @pytest.mark.parametrize("comment_body", [
        "@achord-review please take another look",
        "@achord-review 复审这一轮改动。",
        "已按 P2 建议修复并推送 `5d4e26e`，请 @achord-review 复审这一轮改动。",
        "@achord-review can you re-run this?",
    ])
    def test_prose_after_the_mention_falls_back_to_the_default_command(self, mention_enabled, comment_body):
        # Prose used to be forwarded verbatim as "/复审这一轮改动。", which handle_request
        # rejects as an unknown command before it adds the eyes reaction - no reaction, no
        # review, no error, which looks exactly like the bot being down.
        assert normalize_mention_command(comment_body) == "/review"

    @pytest.mark.parametrize("comment_body, expected", [
        ("@achord-review /describe", "/describe"),
        ("@achord-review /nope", "/nope"),
    ])
    def test_an_explicit_slash_command_is_passed_through(self, mention_enabled, comment_body, expected):
        # An explicit slash is a deliberate command: keep forwarding it, even when unknown,
        # so it fails where the operator can see it rather than being silently rewritten.
        assert normalize_mention_command(comment_body) == expected

    @pytest.mark.parametrize("comment_body", [
        "/review",
        "a comment that does not mention the bot",
        "",
        None,
    ])
    def test_bodies_without_the_mention_are_untouched(self, mention_enabled, comment_body):
        assert normalize_mention_command(comment_body) == comment_body

    def test_default_command_is_configurable(self, mention_enabled):
        get_settings().set("github_app.mention_default_command", "/describe")
        assert normalize_mention_command("@achord-review") == "/describe"
