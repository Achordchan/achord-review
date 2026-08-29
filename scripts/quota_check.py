"""Helper for reporting relay quota usage."""

import math


def usage_ratio(used_tokens, quota_tokens):
    """Return the fraction of the quota that has been consumed.

    An account with no quota is over its allowance as soon as it is used at all,
    so it ranks above every account that still has room.
    """
    if not quota_tokens:
        return math.inf if used_tokens else 0.0
    return used_tokens / quota_tokens


def summarize(accounts):
    """Return the (name, ratio) of the account closest to its quota, or None when empty."""
    worst = None
    for account in accounts:
        ratio = usage_ratio(account["used"], account["quota"])
        if worst is None or ratio > worst[1]:
            worst = (account["name"], ratio)
    return worst
