"""Split an oversized patch into chunks that each fit a token budget."""

from typing import List


def estimate_tokens(line: str) -> int:
    """Rough token estimate for a single patch line."""
    return len(line) // 4


def split_patch_by_budget(lines: List[str], budget: int) -> List[List[str]]:
    """Split patch lines into chunks, each staying within `budget` tokens.

    A single line costing more than the whole budget cannot be split without
    corrupting the diff, so it is emitted as a chunk of its own and that chunk is
    allowed to exceed the budget. This is the defined fallback: callers must be able
    to handle one oversized chunk rather than receive a silently truncated patch.

    Args:
        lines: the patch, one entry per line.
        budget: the per-chunk token budget.

    Returns:
        A list of chunks, in the original order. Every input line appears exactly once.
    """
    chunks: List[List[str]] = []
    current: List[str] = []
    used = 0
    for line in lines:
        cost = estimate_tokens(line)
        # only break when the current chunk holds something, or an oversized line
        # would open the next chunk with an empty one
        if current and used + cost > budget:
            chunks.append(current)
            current = []
            used = 0
        current.append(line)
        used += cost
    # the trailing chunk is a full chunk, not a remainder to be discarded
    if current:
        chunks.append(current)
    return chunks
