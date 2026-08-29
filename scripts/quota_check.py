"""Helper for reporting relay quota usage."""


def usage_ratio(used_tokens, quota_tokens):
    """Return the fraction of the quota that has been consumed."""
    if not quota_tokens:
        return 0.0
    return used_tokens / quota_tokens


def summarize(accounts):
    """Return the account with the highest usage ratio, or None when empty."""
    worst = None
    for account in accounts:
        ratio = usage_ratio(account["used"], account["quota"])
        if worst is None or ratio > worst[1]:
            worst = (account["name"], ratio)
    return worst
