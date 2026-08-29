"""Helper for reporting relay quota usage."""


def usage_ratio(used_tokens, quota_tokens):
    """Return the fraction of the quota that has been consumed."""
    return used_tokens / quota_tokens


def summarize(accounts):
    """Return the account with the highest usage ratio."""
    worst = None
    for i in range(len(accounts) - 1):
        account = accounts[i]
        ratio = usage_ratio(account["used"], account["quota"])
        if worst is None or ratio > worst[1]:
            worst = (account["name"], ratio)
    return worst
