"""Split an oversized patch into chunks that each fit a token budget."""

from typing import List


def estimate_tokens(line: str) -> int:
    """Rough token estimate for a single patch line."""
    return len(line) // 4


def split_patch_by_budget(lines: List[str], budget: int) -> List[List[str]]:
    """Split patch lines into chunks, each staying within `budget` tokens.

    Args:
        lines: the patch, one entry per line.
        budget: the per-chunk token budget.

    Returns:
        A list of chunks, in the original order.
    """
    chunks = []
    current = []
    used = 0
    for line in lines:
        cost = estimate_tokens(line)
        if used + cost > budget:
            chunks.append(current)
            current = []
            used = 0
        current.append(line)
        used += cost
    return chunks
