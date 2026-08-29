import pytest

from pr_agent.servers.github_app import is_fork_pr


def _body(head_repo, base_repo="acme/app"):
    pull_request = {"base": {"repo": {"full_name": base_repo}}}
    if head_repo is not None:
        pull_request["head"] = {"repo": {"full_name": head_repo}}
    else:
        pull_request["head"] = {"repo": None}
    return {"pull_request": pull_request}


class TestIsForkPr:
    @pytest.mark.parametrize("head_repo, expected", [
        ("acme/app", False),          # same repo -> internal branch
        ("contributor/app", True),    # different owner -> fork
        ("acme/other", True),         # different repo name -> fork
    ])
    def test_head_and_base_repo_comparison(self, head_repo, expected):
        assert is_fork_pr(_body(head_repo)) is expected

    @pytest.mark.parametrize("body", [
        {},
        {"pull_request": {}},
        {"pull_request": {"head": {}, "base": {}}},
        _body(None),                  # GitHub omits head.repo for a deleted fork
    ])
    def test_unknown_shape_is_treated_as_a_fork(self, body):
        assert is_fork_pr(body) is True
