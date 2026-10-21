"""Which account takes which piece, for any number of accounts.

The split is even: pieces go round-robin over the ready accounts, in list order, so the first wave reaches every
account at once. Usage is never measured. An account is skipped only while it is marked full (one of its workers
printed the usage-limit message, until the reset time that message names); paused, duplicate and signed-out rows
never reach here.
"""
import accounts as A


def ready(rows, exclude=(), now=None):
    """Names of the rows that may start a worker now, in list order."""
    return [c["name"] for c in rows if c["name"] not in exclude and not A.full_until(c["name"], now)]


def split(n, rows, now=None):
    """The account name for each of n pieces, round-robin; [] if no account is ready."""
    names = ready(rows, now=now)
    if not names or n <= 0:
        return []
    return [names[i % len(names)] for i in range(n)]


def relay(rows, exclude=(), load=None, now=None):
    """The ready account outside `exclude` with the fewest live workers (`load` = {name: count}), else None."""
    names = ready(rows, exclude, now)
    if not names:
        return None
    load = load or {}
    return min(names, key=lambda n: (load.get(n, 0), names.index(n)))


def can(rows, name, now=None):
    """May this account start a worker right now?"""
    return name in ready(rows, now=now)
