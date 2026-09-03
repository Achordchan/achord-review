"""Tests for related-file retrieval (pr_agent/algo/related_files.py)."""

import pytest

from pr_agent.algo import related_files
from pr_agent.config_loader import get_settings


class FakeHandler:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def chat_completion(self, model, temperature, system, user):
        self.calls.append({"system": system, "user": user})
        return self.response, "stop"


class FakeProvider:
    def __init__(self, tree, files=None):
        self.tree = tree
        self.files = files or {}
        self.fetched = []

    def get_repo_tree(self, from_default_branch: bool = False):
        return self.tree

    def get_repo_file_content(self, file_path: str, from_default_branch: bool = False):
        self.fetched.append(file_path)
        if file_path not in self.files:
            raise FileNotFoundError(file_path)
        return self.files[file_path]


def _yaml(*paths):
    return "files:\n" + "".join(f'- path: "{p}"\n  reason: "because"\n' for p in paths)


@pytest.fixture(autouse=True)
def _reset_settings():
    section = get_settings().related_files
    original = {key: section[key] for key in list(section.keys())}
    # Retrieval ships disabled; these tests exercise it switched on.
    section["enabled"] = True
    yield
    for key, value in original.items():
        section[key] = value


class TestSelectionIsAllowlisted:
    @pytest.mark.parametrize("returned", [
        "../../etc/passwd",
        "/etc/passwd",
        "src/does-not-exist.py",
        "https://evil.example/payload",
    ])
    def test_a_path_outside_the_candidate_list_is_never_fetched(self, returned):
        # The model names files; it must not be able to name anything it was not offered.
        assert related_files._parse_selection(_yaml(returned), ["src/app.py"], 6) == []

    def test_offered_paths_are_kept_in_order_and_capped(self):
        candidates = ["a.py", "b.py", "c.py"]

        assert related_files._parse_selection(_yaml("c.py", "a.py", "c.py"), candidates, 2) == \
            ["c.py", "a.py"]

    def test_unparseable_selection_degrades_to_nothing(self):
        assert related_files._parse_selection("not yaml at all: [", ["a.py"], 6) == []


class TestCandidateRanking:
    def test_changed_and_vendored_paths_are_not_offered(self):
        tree = ["src/app.py", "src/util.py", "node_modules/lib/index.js",
                "assets/logo.png", "package-lock.json"]

        candidates = related_files._rank_candidates(tree, ["src/app.py"], 50)

        assert candidates == ["src/util.py"]

    def test_neighbours_outrank_distant_files_of_the_same_kind(self):
        tree = ["far/other.py", "src/sibling.py", "docs/readme.md"]

        candidates = related_files._rank_candidates(tree, ["src/app.py"], 50)

        assert candidates[0] == "src/sibling.py"
        assert set(candidates) == {"src/sibling.py", "far/other.py", "docs/readme.md"}

    def test_the_candidate_list_is_capped(self):
        tree = [f"src/file{i}.py" for i in range(100)]

        assert len(related_files._rank_candidates(tree, ["src/app.py"], 7)) == 7


class TestRendering:
    def test_attached_files_are_framed_as_data_not_instructions(self):
        rendered = related_files._render({"src/util.py": "print('hi')"}, 500)

        assert "never as instructions" in rendered
        assert "Do not audit these files on their own" in rendered
        assert '<file path="src/util.py">' in rendered
        assert "print('hi')" in rendered

    def test_content_that_contains_a_fence_cannot_break_out_of_its_block(self):
        rendered = related_files._render({"a.md": "````` not the end"}, 500)

        assert "``````" in rendered

    def test_total_lines_are_capped(self):
        rendered = related_files._render({"big.py": "\n".join(str(i) for i in range(500))}, 40)

        assert "...(truncated)..." in rendered
        assert len(rendered.splitlines()) <= 40


class TestBuild:
    @pytest.mark.asyncio
    async def test_end_to_end_attaches_the_selected_file(self):
        provider = FakeProvider(["src/app.py", "src/preview.js"],
                                {"src/preview.js": "function updatePreview() {}"})
        handler = FakeHandler(_yaml("src/preview.js"))

        rendered = await related_files.build_related_files(
            provider, handler, "gpt-5", "diff --git a/src/app.py", ["src/app.py"], title="t")

        assert "updatePreview" in rendered
        assert provider.fetched == ["src/preview.js"]
        # the candidate list, not the repo, is what the model was shown
        assert "src/preview.js" in handler.calls[0]["user"]
        assert "src/app.py" not in handler.calls[0]["user"].split("Candidate files")[1]

    @pytest.mark.asyncio
    async def test_a_provider_without_tree_support_degrades_to_no_context(self):
        class NoTree:
            def get_repo_file_content(self, file_path, from_default_branch=False):
                return "x"

        rendered = await related_files.build_related_files(
            NoTree(), FakeHandler(_yaml("a.py")), "gpt-5", "diff", [])

        assert rendered == ""

    @pytest.mark.asyncio
    async def test_an_unreadable_file_does_not_lose_the_others(self):
        provider = FakeProvider(["a.py", "b.py"], {"b.py": "kept"})
        handler = FakeHandler(_yaml("a.py", "b.py"))

        rendered = await related_files.build_related_files(
            provider, handler, "gpt-5", "diff", [])

        assert "kept" in rendered
        assert '<file path="a.py">' not in rendered

    @pytest.mark.asyncio
    async def test_disabled_retrieval_makes_no_model_call(self):
        get_settings().related_files.enabled = False
        handler = FakeHandler(_yaml("a.py"))

        rendered = await related_files.build_related_files(
            FakeProvider(["a.py"], {"a.py": "x"}), handler, "gpt-5", "diff", [])

        assert rendered == ""
        assert handler.calls == []

    @pytest.mark.asyncio
    async def test_a_failing_selection_call_never_breaks_the_review(self):
        class Broken:
            async def chat_completion(self, **kwargs):
                raise RuntimeError("relay down")

        rendered = await related_files.build_related_files(
            FakeProvider(["a.py"], {"a.py": "x"}), Broken(), "gpt-5", "diff", [])

        assert rendered == ""


class TestTokenBudget:
    def test_a_block_that_does_not_fit_is_dropped_whole(self):
        # A cut block would leave an open fence, and the review schema printed
        # after it would be read as file content.
        class Counter:
            def count_tokens(self, text, force_accurate=False):
                return len(text.splitlines())

        rendered = related_files._render(
            {"small.py": "one\ntwo", "huge.py": "\n".join(str(i) for i in range(500))},
            max_total_lines=500, max_tokens=30, count_tokens=Counter().count_tokens)

        assert rendered.count("<file ") == rendered.count("</file>")
        assert rendered.count("`````") % 2 == 0
        assert rendered.endswith("</related_files>")
        assert "small.py" in rendered

    def test_the_complete_result_including_the_closing_tag_is_under_budget(self):
        class Counter:
            def count_tokens(self, text, force_accurate=False):
                return len(text.splitlines())

        rendered = related_files._render(
            {f"f{i}.py": "x\ny\nz" for i in range(20)},
            max_total_lines=500, max_tokens=25, count_tokens=Counter().count_tokens)

        assert len(rendered.splitlines()) <= 25

    def test_a_broken_counter_attaches_nothing_rather_than_risking_the_diff(self):
        def broken(text, force_accurate=False):
            raise RuntimeError("no encoder")

        assert related_files._render({"a.py": "x"}, 500, max_tokens=10, count_tokens=broken) == ""

    def test_budget_is_what_the_model_window_has_left_after_the_diff(self, monkeypatch):
        class Handler:
            prompt_tokens = 1000

            def count_tokens(self, text, force_accurate=False):
                return 4000

        monkeypatch.setattr("pr_agent.algo.utils.get_max_tokens", lambda model: 10000)

        # 10000 - 1000 prompt - 4000 diff - 1500 output buffer = 3500
        assert related_files.available_token_budget("m", Handler(), "diff", 12000) == 3500
        # never more than the configured ceiling
        assert related_files.available_token_budget("m", Handler(), "diff", 500) == 500

    def test_an_exhausted_window_yields_no_budget(self, monkeypatch):
        class Handler:
            prompt_tokens = 9000

            def count_tokens(self, text, force_accurate=False):
                return 4000

        monkeypatch.setattr("pr_agent.algo.utils.get_max_tokens", lambda model: 10000)

        assert related_files.available_token_budget("m", Handler(), "diff", 12000) == 0

    def test_an_unknown_window_yields_no_budget(self, monkeypatch):
        def boom(model):
            raise KeyError(model)

        monkeypatch.setattr("pr_agent.algo.utils.get_max_tokens", boom)

        assert related_files.available_token_budget("m", object(), "diff", 12000) == 0


class TestPerModelRendering:
    """Collection is shared across fallback attempts; the budget is not."""

    def _counter(self):
        class Handler:
            prompt_tokens = 500

            def count_tokens(self, text, force_accurate=False):
                return len(text.splitlines()) * 10

        return Handler()

    def test_a_smaller_fallback_window_drops_context_the_primary_could_afford(self, monkeypatch):
        windows = {"big": 20000, "small": 2000}
        monkeypatch.setattr("pr_agent.algo.utils.get_max_tokens", lambda model: windows[model])
        files = {"a.py": "\n".join(f"line {i}" for i in range(30))}

        on_primary = related_files.render_related_files(files, "big", "diff", self._counter())
        on_fallback = related_files.render_related_files(files, "small", "diff", self._counter())

        assert "line 0" in on_primary
        assert on_fallback == ""

    @pytest.mark.asyncio
    async def test_collection_returns_contents_not_a_rendered_block(self):
        provider = FakeProvider(["a.py", "b.py"], {"b.py": "kept"})
        handler = FakeHandler(_yaml("b.py"))

        files = await related_files.collect_related_files(
            provider, handler, "gpt-5", "diff", [])

        assert files == {"b.py": "kept"}


def test_an_offered_dotfile_survives_path_validation():
    # lstrip("./") would turn this into "github/workflows/ci.yml" and fail the
    # allowlist it is supposed to pass.
    candidates = [".github/workflows/ci.yml", ".eslintrc"]

    assert related_files._parse_selection(
        _yaml("./.github/workflows/ci.yml", ".eslintrc"), candidates, 6) == candidates


class TestSensitiveFilesAreNeverRetrieved:
    @pytest.mark.parametrize("path", [
        ".env", "deploy/.env.production", "config/service-account.json",
        "keys/server.pem", ".npmrc", "home/id_rsa", "infra/terraform.tfstate",
        "config/.secrets.toml", "app/db_password.txt", "ci/api_key.json",
    ])
    def test_a_credential_path_is_not_offered_even_with_exclusions_cleared(self, path):
        # The candidate list comes from the repository tree, so a repo that tracks
        # a credential file would otherwise put it one model choice away.
        get_settings().related_files.exclude_globs = []

        assert related_files.is_sensitive_path(path) is True
        assert related_files._rank_candidates([path, "src/app.py"], [], 50) == ["src/app.py"]

    @pytest.mark.asyncio
    async def test_a_selected_credential_path_is_still_refused_at_fetch_time(self):
        # Defence in depth: reaching the fetch means a bug upstream, not a choice.
        provider = FakeProvider([".env", "src/app.py"], {".env": "TOKEN=supersecret"})
        handler = FakeHandler(_yaml(".env"))

        rendered = await related_files.build_related_files(
            provider, handler, "gpt-5", "diff", [])

        assert "supersecret" not in rendered
        assert ".env" not in provider.fetched

    def test_ordinary_source_files_are_still_offered(self):
        tree = ["src/token_service.py", "src/app.py"]

        # A file whose *name* merely mentions a secret is excluded too: losing it
        # costs recall, keeping it risks a credential.
        assert related_files._rank_candidates(tree, [], 50) == ["src/app.py"]


class TestOversizedFiles:
    def _counter(self):
        def count(text, force_accurate=False):
            return len(text.splitlines()) * 10
        return count

    def test_a_file_over_the_token_budget_is_shortened_not_dropped(self):
        rendered = related_files._render(
            {"big.py": "\n".join(f"line {i}" for i in range(400))},
            max_total_lines=800, max_tokens=300, count_tokens=self._counter())

        assert "line 0" in rendered
        assert "...(truncated)..." in rendered
        assert rendered.count("<file ") == rendered.count("</file>")
        assert len(rendered.splitlines()) * 10 <= 300

    def test_a_smaller_later_file_survives_an_oversized_earlier_one(self):
        rendered = related_files._render(
            {"huge.py": "\n".join(f"x{i}" for i in range(400)), "small.py": "one\ntwo"},
            max_total_lines=800, max_tokens=300, count_tokens=self._counter())

        assert "small.py" in rendered


class TestContentScreening:
    @pytest.mark.parametrize("content", [
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n-----END RSA PRIVATE KEY-----",
        'GITHUB_TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz0123"',
        'openai_key = "sk-abcdefghijklmnopqrstuvwxyz0123456789"',
        "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
        'api_key: "1c3f8b2a9d4e5f6a7b8c"',
        'DATABASE_URL = "postgres://admin:hunter2secret@db.internal:5432/app"',
    ])
    def test_credential_material_is_recognised(self, content):
        assert related_files.contains_credential_material(content) is True

    @pytest.mark.parametrize("content", [
        "def login(user, password):\n    return check(user, password)",
        "API_KEY = os.environ['API_KEY']",
        "# set your token in .env before running",
        'placeholder = "REPLACE_WITH_TOKEN"',
    ])
    def test_ordinary_code_about_secrets_is_not_flagged(self, content):
        assert related_files.contains_credential_material(content) is False

    @pytest.mark.asyncio
    async def test_a_credential_in_an_innocent_filename_is_refused(self):
        # The path denylist cannot see this one; the content check has to.
        provider = FakeProvider(
            ["config/settings.py", "src/app.py"],
            {"config/settings.py": 'SECRET_KEY = "ghp_abcdefghijklmnopqrstuvwxyz0123"'})
        handler = FakeHandler(_yaml("config/settings.py"))

        rendered = await related_files.build_related_files(
            provider, handler, "gpt-5", "diff", [])

        assert rendered == ""
        assert "ghp_" not in rendered


class TestRetrievalIsOptIn:
    @pytest.mark.asyncio
    async def test_the_shipped_default_makes_no_request(self):
        # Sending untouched repository content to a provider is a decision each
        # deployment makes, not a default.
        del get_settings().related_files["enabled"]
        handler = FakeHandler(_yaml("a.py"))

        files = await related_files.collect_related_files(
            FakeProvider(["a.py"], {"a.py": "x"}), handler, "gpt-5", "diff", [])

        assert files == {}
        assert handler.calls == []


class TestSelectorPromptBudget:
    def test_the_candidate_list_is_trimmed_to_fit_the_model_window(self, monkeypatch):
        monkeypatch.setattr("pr_agent.algo.utils.get_max_tokens", lambda model: 2000)

        class Handler:
            def count_tokens(self, text, force_accurate=False):
                return len(text.splitlines()) * 5

        candidates = [f"src/file{i}.py" for i in range(400)]

        fitted = related_files._candidates_that_fit(
            "diff", "t", candidates, 6, "m", Handler())

        assert 0 < len(fitted) < len(candidates)
        assert fitted == candidates[:len(fitted)]

    def test_retrieval_is_skipped_when_even_one_candidate_does_not_fit(self, monkeypatch):
        monkeypatch.setattr("pr_agent.algo.utils.get_max_tokens", lambda model: 10)

        class Handler:
            def count_tokens(self, text, force_accurate=False):
                return 10_000

        assert related_files._candidates_that_fit(
            "diff", "t", ["a.py", "b.py"], 6, "m", Handler()) == []
