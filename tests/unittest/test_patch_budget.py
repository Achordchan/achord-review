import pytest

from pr_agent.algo.patch_budget import split_patch_by_budget


def _flatten(chunks):
    return [line for chunk in chunks for line in chunk]


class TestSplitPatchByBudget:
    def test_every_line_survives_the_split(self):
        lines = [f"+ line {i} " * 3 for i in range(20)]
        assert _flatten(split_patch_by_budget(lines, 20)) == lines

    def test_a_patch_within_budget_is_one_chunk(self):
        lines = ["+ a", "+ b"]
        assert split_patch_by_budget(lines, 1000) == [lines]

    def test_no_chunk_is_empty(self):
        lines = ["x" * 400, "+ a", "y" * 400]
        assert all(chunk for chunk in split_patch_by_budget(lines, 10))

    def test_an_oversized_line_gets_its_own_chunk(self):
        huge = "z" * 4000
        chunks = split_patch_by_budget(["+ a", huge, "+ b"], 10)
        assert [huge] in chunks

    @pytest.mark.parametrize("lines", [[], ["+ only"]])
    def test_degenerate_inputs(self, lines):
        assert _flatten(split_patch_by_budget(lines, 5)) == lines
