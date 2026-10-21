"""The accounts of the multi-account plugin.

One list, kept in the plugin's data folder (<data>/accounts.json). Each row is one Claude Code config folder with
its own login, connected one at a time:

    {"name": "work2", "dir": "~/.claude-work2", "email": "...", "uuid": "...", "active": true,
     "duplicate_of": null, "since": "2026-10-21 15:02"}

An account is known by its accountUuid, never by its folder: two folders can hold the same login, so a login is only
counted once ("duplicate of main").

This plugin never reads, stores or sends a login secret. It identifies an account only from the non-secret
`oauthAccount` block (email and accountUuid) that Claude Code writes in the folder's `.claude.json` at sign-in:
signed in = that block has an accountUuid. Usage is never measured: an account is "full" only after one of its
workers printed Claude Code's usage-limit message, until the reset time that message names (or one hour).

Data folder: $MULTI_ACCOUNT_HOME, else ~/.multi-account (outside every .claude folder, so workers may write
reports there). Tests replace the machine with MA_HOME (the home
folder) and MA_NO_OPEN (never open a window).
"""
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager

HOME = os.environ.get("MA_HOME") or os.path.expanduser("~")
DATA = os.environ.get("MULTI_ACCOUNT_HOME") or os.path.join(HOME, ".multi-account")
LIST = os.path.join(DATA, "accounts.json")
FULL = os.path.join(DATA, "full.json")
MAIN = "~/.claude"
NAME_OK = re.compile(r"^[a-z0-9][a-z0-9-]{0,19}$")
RESERVED = {"main", "multi-account", "all"}
FULL_DEFAULT_S = 3600  # a limit message with no readable reset time keeps the account out for one hour


# ---------- folders ----------

def short(d):
    """The stored spelling: '~/.claude', '~/.claude-work2'."""
    d = (d or MAIN).rstrip("/")
    if d.startswith(HOME + "/") or d == HOME:
        d = "~" + d[len(HOME):]
    return d


def path(d):
    """The absolute folder."""
    d = short(d)
    return HOME + d[1:] if d.startswith("~") else d


def is_main(d):
    return short(d) == MAIN


def config_env(d):
    """The CLAUDE_CONFIG_DIR value for that account ('' = leave it unset: the main account)."""
    return "" if is_main(d) else path(d)


def identity(d):
    """(email, accountUuid) from that folder's .claude.json, written by Claude Code at sign-in; (None, None) if none.
    Only the non-secret oauthAccount block is read."""
    f = os.path.join(HOME, ".claude.json") if is_main(d) else os.path.join(path(d), ".claude.json")
    try:
        a = json.load(open(f)).get("oauthAccount") or {}
        return a.get("emailAddress"), a.get("accountUuid")
    except Exception:
        return None, None


def signed_in(d):
    return bool(identity(d)[1])


# ---------- time ----------

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}


def hour(ts):
    """Local time, as the user reads it: "today 18:50" or "Wed 28 14:00"."""
    if not ts:
        return "-"
    t = time.localtime(ts)
    today = time.localtime()
    if t.tm_yday == today.tm_yday and t.tm_year == today.tm_year:
        return "today %s" % time.strftime("%H:%M", t)
    return "%s %d %s" % (DAYS[t.tm_wday], t.tm_mday, time.strftime("%H:%M", t))


RESET_RE = re.compile(r"resets?\s+(?:at\s+)?(?:(?P<mon>[A-Za-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+"
                      r"(?:at\s+)?)?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap]\.?m\.?)?", re.I)


def reset_from_message(text, now=None):
    """When a full account takes work again, read from Claude Code's limit message ("... resets 5pm",
    "resets Oct 22, 9:30am"), in local time. None when the message names no time."""
    now = now or time.time()
    m = RESET_RE.search(text or "")
    if not m:
        return None
    h, mi = int(m.group("h")), int(m.group("m") or 0)
    ap = (m.group("ap") or "").lower().replace(".", "")
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    t = time.localtime(now)
    year, mon, day = t.tm_year, t.tm_mon, t.tm_mday
    dated = m.group("mon") and m.group("mon").lower()[:3] in MONTHS
    if dated:
        mon, day = MONTHS[m.group("mon").lower()[:3]], int(m.group("day"))
    ts = time.mktime((year, mon, day, h, mi, 0, 0, 0, -1))
    if dated and ts < now - 86400:
        ts = time.mktime((year + 1, mon, day, h, mi, 0, 0, 0, -1))
    while not dated and ts <= now:
        ts += 86400
    return ts


# ---------- full marks (from limit messages) ----------

def _read(f, empty):
    try:
        return json.load(open(f))
    except Exception:
        return empty


def _write(f, data):
    os.makedirs(os.path.dirname(f), exist_ok=True)
    tmp = "%s.%d" % (f, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, f)


def mark_full(name, message, now=None):
    """A worker on this account printed the usage-limit message: keep it out until the reset it names."""
    now = now or time.time()
    until = reset_from_message(message, now) or now + FULL_DEFAULT_S
    with lock():
        marks = _read(FULL, {})
        marks[name] = {"until": until, "since": now, "message": (message or "").strip()[:200]}
        _write(FULL, marks)
    return until


def full_until(name, now=None):
    """The reset time of a full account, or None once that time has passed (it takes work again)."""
    now = now or time.time()
    m = _read(FULL, {}).get(name)
    return m["until"] if m and m.get("until", 0) > now else None


# ---------- the list ----------

@contextmanager
def lock():
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, ".lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _now_text():
    return time.strftime("%Y-%m-%d %H:%M")


def _candidates():
    """~/.claude first, then every ~/.claude-<name> folder that is signed in, by name."""
    out = [MAIN]
    try:
        names = sorted(os.listdir(HOME))
    except OSError:
        names = []
    for n in names:
        if n.startswith(".claude-") and NAME_OK.match(n[8:]) and n[8:] not in RESERVED \
                and os.path.isdir(os.path.join(HOME, n)) and signed_in("~/" + n):
            out.append("~/" + n)
    return out


def name_of(d):
    return "main" if is_main(d) else short(d).split(".claude-", 1)[1]


def _duplicates(lst):
    """The first row with a uuid owns it; a later row with the same uuid is a duplicate of the first."""
    owners, changed = {}, False
    for c in lst["accounts"]:
        before = c.get("duplicate_of")
        u = c.get("uuid")
        c["duplicate_of"] = owners[u] if u and u in owners else None
        if u and u not in owners:
            owners[u] = c["name"]
        changed |= before != c["duplicate_of"]
    return changed


def _refresh(lst):
    """Fill in email and uuid for rows that signed in (or out) since; True if anything changed."""
    changed = False
    for c in lst["accounts"]:
        email, uuid = identity(c["dir"])
        if (uuid, email) != (c.get("uuid"), c.get("email")) and (uuid or c.get("uuid")):
            c["uuid"], c["email"] = uuid, email if uuid else None
            changed = True
    return _duplicates(lst) or changed


def _import():
    lst = {"version": 1, "accounts": []}
    for d in _candidates():
        email, uuid = identity(d)
        lst["accounts"].append({"name": name_of(d), "dir": short(d), "email": email, "uuid": uuid,
                                "active": True, "duplicate_of": None, "since": _now_text()})
    _duplicates(lst)
    return lst


def load():
    """The list, created on first use from the folders that are already signed in, refreshed on every read."""
    with lock():
        try:
            lst = json.load(open(LIST))
            new = False
        except FileNotFoundError:
            lst, new = _import(), True
        if _refresh(lst) or new:
            _write(LIST, lst)
        return lst


def row(lst, name):
    return next((c for c in lst["accounts"] if c["name"] == name), None)


def state(c, now=None):
    """(key, label) of one row: duplicate, not-signed-in, paused, full, ready."""
    if c.get("duplicate_of"):
        return "duplicate", "duplicate of %s" % c["duplicate_of"]
    if not signed_in(c["dir"]):
        return "not-signed-in", "not signed in"
    if not c.get("active", True):
        return "paused", "paused"
    until = full_until(c["name"], now)
    if until:
        return "full", "full until %s" % hour(until)
    return "ready", "ready"


def available(lst=None):
    """Every row that may take work: signed in, active, not a duplicate (full ones included; split.py skips them)."""
    lst = lst or load()
    return [c for c in lst["accounts"]
            if not c.get("duplicate_of") and c.get("active", True) and signed_in(c["dir"])]


# ---------- connect one account at a time ----------

def _next_name(lst):
    used = {c["name"] for c in lst["accounts"]}
    n = 2
    while "account%d" % n in used or os.path.lexists(os.path.join(HOME, ".claude-account%d" % n)):
        n += 1
    return "account%d" % n


def login_command(name):
    return 'CLAUDE_CONFIG_DIR="$HOME/.claude-%s" claude' % name


def connect(name=None):
    """Add ONE account (or check the one named): an empty config folder of its own. Returns a dict the CLI prints."""
    if name is not None and (not NAME_OK.match(name) or name in RESERVED - {"main"}):
        raise ValueError("unusual name: use lowercase letters, digits or dashes (20 at most), not 'all' or "
                         "'multi-account'")
    with lock():
        try:
            lst = json.load(open(LIST))
        except FileNotFoundError:
            lst = _import()
        _refresh(lst)
        c = row(lst, name) if name else None
        new = c is None
        if new:
            name = name or _next_name(lst)
            if name == "main":
                raise ValueError("'main' is already ~/.claude")
            d = "~/.claude-" + name
            # an empty folder: Claude Code fills it at first start; nothing is copied or linked from another account
            os.makedirs(path(d), mode=0o700, exist_ok=True)
            c = {"name": name, "dir": d, "email": None, "uuid": None, "active": True, "duplicate_of": None,
                 "since": _now_text()}
            lst["accounts"].append(c)
            _refresh(lst)
        _write(LIST, lst)
    key, text = state(c)
    return {"name": name, "dir": c["dir"], "new": new, "state": key, "text": text, "email": c.get("email"),
            "duplicate_of": c.get("duplicate_of"), "login": None if is_main(c["dir"]) else login_command(name)}


def open_terminal(name):
    """Open a Terminal window already inside the new account, so the user only signs in. macOS only."""
    if os.environ.get("MA_NO_OPEN") or sys.platform != "darwin" or not shutil.which("osascript"):
        return False
    cmd = login_command(name).replace('"', '\\"')
    script = 'tell application "Terminal"\nactivate\ndo script "%s"\nend tell' % cmd
    return subprocess.run(["osascript", "-e", script], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def change(name, action):
    """pause | resume | remove. remove only takes the row off the list: the folder and its login stay."""
    with lock():
        lst = json.load(open(LIST))
        c = row(lst, name)
        if c is None:
            raise ValueError("there is no account '%s'" % name)
        if action == "remove":
            if is_main(c["dir"]):
                raise ValueError("the main account cannot be removed; pause it instead")
            lst["accounts"].remove(c)
            _duplicates(lst)
        else:
            c["active"] = action == "resume"
        _write(LIST, lst)
    return c
