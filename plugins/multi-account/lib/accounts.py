"""The accounts of the multi-account plugin.

One list, kept in the plugin's data folder (<data>/accounts.json). Each row is one Claude Code config folder with
its own login, connected one at a time:

    {"name": "work2", "dir": "~/.claude-work2", "email": "...", "login": "...", "active": true,
     "duplicate_of": null, "since": "2026-10-21 15:02"}

An account is known by its login (the email it signed in with), never by its folder: two folders can hold the same
login, so a login is only counted once ("duplicate of main").

This plugin never reads, stores or sends a login secret, and it never opens Claude Code's own files. It asks Claude
Code itself who is signed in, with `claude auth status --json` run for that folder, and keeps only the email from
the answer. Usage is never measured: an account is "full" only after one of its workers printed Claude Code's
usage-limit message, until the reset time that message names (or one hour).

Inside a Claude Code session it also reads the email Claude Code gives its own tools (CLAUDE_CODE_USER_EMAIL, no
secret), to mark the account this session runs on. In the Claude app that email also settles one case: the app writes
its own account into ~/.claude, so when another folder is signed in to that same account, that folder owns it and the
main row's own login is unknown from here (it takes no work).

Data folder: $MULTI_ACCOUNT_HOME, else ~/.multi-account (outside every .claude folder, so workers may write
reports there). Tests replace the machine with MA_HOME (the home
folder), MA_CLAUDE (the `claude` command) and MA_NO_OPEN (never open a window).
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
CLAUDE = os.environ.get("MA_CLAUDE") or "claude"
WHO_TTL_S = 30  # a long-lived watcher asks again after this, so a sign-in or sign-out is seen


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


def clean_env(d):
    """The environment of a `claude` run for account d: a short fixed list of this one's variables, each named here
    (what a program needs to run: path, home, locale, temp folder), never the whole environment. So the parent
    session's variables (its session id, any Anthropic setting) never reach the run, or it would act as the parent's
    account whatever CLAUDE_CONFIG_DIR says. Proxy and certificate settings are not passed either: Claude Code reads
    them from the `env` of that account's own settings.json. CLAUDE_CONFIG_DIR is set to that folder (left unset for
    the main account)."""
    keep = {"PATH": os.environ.get("PATH"), "HOME": os.environ.get("HOME"), "USER": os.environ.get("USER"),
            "LOGNAME": os.environ.get("LOGNAME"), "SHELL": os.environ.get("SHELL"), "TERM": os.environ.get("TERM"),
            "LANG": os.environ.get("LANG"), "LC_ALL": os.environ.get("LC_ALL"), "LC_CTYPE": os.environ.get("LC_CTYPE"),
            "TMPDIR": os.environ.get("TMPDIR"), "TZ": os.environ.get("TZ")}
    env = {k: v for k, v in keep.items() if v is not None}
    cfg = config_env(d)
    if cfg:
        env["CLAUDE_CONFIG_DIR"] = cfg
    return env


_WHO = {}  # folder -> (when asked, (email, login))


def identity(d):
    """(email, login) of the account signed in in that folder, from `claude auth status --json`; (None, None) when
    nobody is signed in there with a Claude account. The login is the email in lower case."""
    d = short(d)
    hit = _WHO.get(d)
    if hit and time.time() - hit[0] < WHO_TTL_S:
        return hit[1]
    who = (None, None)
    if os.path.isdir(path(d)):
        try:
            r = subprocess.run([CLAUDE, "auth", "status", "--json"], capture_output=True, text=True, timeout=30,
                               env=clean_env(d), stdin=subprocess.DEVNULL)
            a = json.loads(r.stdout or "{}")
            email = a.get("email") if a.get("loggedIn") else None
            if isinstance(email, str) and "@" in email:
                who = (email, email.strip().lower())
        except Exception:
            pass
    _WHO[d] = (time.time(), who)
    return who


def signed_in(d):
    return bool(identity(d)[1])


def this_session():
    """The login of the account this Claude Code session runs on, from the email Claude Code gives its own tools; None
    outside a session."""
    e = (os.environ.get("CLAUDE_CODE_USER_EMAIL") or "").strip().lower()
    return e if "@" in e else None


def in_app():
    """True inside the Claude app (its sessions name the app version)."""
    return bool(os.environ.get("CLAUDE_CODE_DESKTOP_APP_VERSION"))


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
    named = reset_from_message(message, now)
    until = named or now + FULL_DEFAULT_S
    with lock():
        marks = _read(FULL, {})
        marks[name] = {"until": until, "since": now, "named": bool(named), "message": (message or "").strip()[:200]}
        _write(FULL, marks)
    return until


def full_mark(name, now=None):
    """The full mark of an account ({until, since, named, message}), or None once its reset time has passed."""
    now = now or time.time()
    m = _read(FULL, {}).get(name)
    return m if m and m.get("until", 0) > now else None


def full_until(name, now=None):
    """The reset time of a full account, or None once that time has passed (it takes work again)."""
    m = full_mark(name, now)
    return m["until"] if m else None


def full_label(m):
    """"full until <reset> (limit seen <time>)": when the limit was seen, and when the account takes work again."""
    seen = "limit seen %s" % hour(m["since"]) if m.get("since") else ""
    if m.get("named") is False:
        seen += ", no reset time in the message: one hour"
    return "full until %s%s" % (hour(m["until"]), " (%s)" % seen if seen else "")


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


def app_shadowed(lst):
    """The main row when, inside the Claude app, it only shows the app's account: the app writes its own account into
    ~/.claude, so when another folder is signed in to that account, that folder owns it and the login a `claude` run in
    ~/.claude uses is unknown from here. None otherwise."""
    app = this_session() if in_app() else None
    m = next((c for c in lst["accounts"] if is_main(c["dir"])), None)
    if app and m and m.get("login") == app and any(c is not m and c.get("login") == app for c in lst["accounts"]):
        return m
    return None


def _duplicates(lst):
    """The first row with a login owns it; a later row with the same login is a duplicate of the first. A main row the
    Claude app shadows comes last, so the other folder owns the app's account."""
    owners, changed = {}, False
    shadow = app_shadowed(lst)
    for c in sorted(lst["accounts"], key=lambda c: c is shadow):  # stable: only the shadowed main row moves
        before = c.get("duplicate_of")
        u = c.get("login")
        c["duplicate_of"] = owners[u] if u and u in owners else None
        if u and u not in owners:
            owners[u] = c["name"]
        changed |= before != c["duplicate_of"]
    return changed


def _refresh(lst):
    """Fill in email and login for rows that signed in (or out) since; True if anything changed."""
    changed = False
    for c in lst["accounts"]:
        email, login = identity(c["dir"])
        if (login, email) != (c.get("login"), c.get("email")) and (login or c.get("login")):
            c["login"], c["email"] = login, email if login else None
            changed = True
    return _duplicates(lst) or changed


def _import():
    lst = {"version": 1, "accounts": []}
    for d in _candidates():
        email, login = identity(d)
        lst["accounts"].append({"name": name_of(d), "dir": short(d), "email": email, "login": login,
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
    """(key, label) of one row: unknown, duplicate, not-signed-in, paused, full, ready."""
    if c.get("duplicate_of"):
        if is_main(c["dir"]) and in_app() and c.get("login") == this_session():
            return "unknown", "login unknown in the Claude app"
        return "duplicate", "duplicate of %s" % c["duplicate_of"]
    if not signed_in(c["dir"]):
        return "not-signed-in", "not signed in"
    if not c.get("active", True):
        return "paused", "paused"
    m = full_mark(c["name"], now)
    if m:
        return "full", full_label(m)
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
            c = {"name": name, "dir": d, "email": None, "login": None, "active": True, "duplicate_of": None,
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


def set_role(name, role):
    """Give an account a role (plan, design, dev or audit: it then takes only that stage), or None to take it away."""
    with lock():
        lst = json.load(open(LIST))
        c = row(lst, name)
        if c is None:
            raise ValueError("there is no account '%s'" % name)
        if role:
            c["role"] = role
        else:
            c.pop("role", None)
        _write(LIST, lst)
    return c
