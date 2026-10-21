"""A run: one task split into pieces, every piece on its own account at the same time.

Each piece gets its own git worktree and branch (absolute paths), and a headless `claude -p` on the account the split
picked (CLAUDE_CONFIG_DIR). A worker is fenced in: it may read, edit and write files (acceptEdits) only inside its own
worktree, write its report in the run's report folder (--add-dir), run git only as `git -C <its worktree>`, and run
only the quick check its piece names. Nothing else is allowed, and a headless worker cannot be asked, so anything
outside that list is refused.

A watcher checks every PAUSE_S seconds:
  - commit + report "RESULT: DONE"  -> the piece is done
  - the usage-limit message          -> the account is marked full until the reset the message names, and the piece
                                        moves to another ready account, which continues in the same worktree from the
                                        report and the commits
  - ended with nothing               -> one retry on another account; a second failure is FAILED, with its reason
When every piece is done or failed, join.py merges the branches in a separate worktree.

State: <data>/runs/<id>/run.json (+ logs/, pids.txt, watcher.log, REPORT.md). Worktrees and the workers' reports live
in <data>/worktrees/<id>/ (reports in _reports/).
"""
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid as _uuid

import accounts as A
import split as S

RUNS = os.path.join(A.DATA, "runs")
WORKTREES = os.environ.get("MULTI_ACCOUNT_WORKTREES") or os.path.join(A.DATA, "worktrees")
CLAUDE = os.environ.get("MA_CLAUDE") or "claude"
MAX_LOCAL = int(os.environ.get("MA_MAX_LOCAL") or 6)  # never more workers at once on this machine
PAUSE_S = float(os.environ.get("MA_PAUSE") or 30)
LIMIT_RE = re.compile(r"hit your (weekly|session|usage|opus|sonnet)? ?limit|usage limit reached|limit reached", re.I)
ID_OK = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
# the parent session's variables never reach a worker: a token or a session id in them would make every worker run
# as the parent's account, whatever CLAUDE_CONFIG_DIR says
ENV_OUT = re.compile(r"^(CLAUDE_CODE_|CLAUDECODE$|CLAUDE_PID$|CLAUDE_EFFORT$|ANTHROPIC_API_KEY$|ANTHROPIC_AUTH_TOKEN$)")
TOOLS = ["Read", "Edit", "Write", "Glob", "Grep"]  # plus git in its own worktree and its piece's check


def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(args), (r.stderr or r.stdout).strip()[:300]))
    return r.stdout.strip() if check else r


def now_text():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------- state ----------

def folder(rid):
    return os.path.join(RUNS, rid)


def load(rid):
    return json.load(open(os.path.join(folder(rid), "run.json")))


def save(run):
    f = os.path.join(folder(run["id"]), "run.json")
    tmp = "%s.%d" % (f, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(run, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, f)


def note(run, text):
    with open(os.path.join(folder(run["id"]), "watcher.log"), "a") as f:
        f.write("%s %s\n" % (now_text(), text))


def last():
    try:
        ids = sorted(n for n in os.listdir(RUNS) if os.path.exists(os.path.join(RUNS, n, "run.json")))
    except OSError:
        return None
    return ids[-1] if ids else None


# ---------- the plan ----------

def _overlap(a, b):
    a, b = a.strip("/"), b.strip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def validate(plan):
    pieces = plan.get("pieces") or []
    if not pieces:
        raise ValueError("the plan has no pieces")
    seen = set()
    for p in pieces:
        if not ID_OK.match(str(p.get("id", ""))) or p["id"] in seen or p["id"] == "joined":
            raise ValueError("unusual or repeated piece id: %r" % p.get("id"))
        seen.add(p["id"])
        if not p.get("todo") or not p.get("files"):
            raise ValueError("piece %s needs its todo and its files" % p["id"])
        for f in p["files"]:
            if f.startswith("/") or ".." in f.split("/"):
                raise ValueError("piece %s: files are relative to the project (%s)" % (p["id"], f))
        if p.get("check") and re.search(r"[;&|`$<>\n]", p["check"]):
            raise ValueError("piece %s: the check is one simple command, with no ; & | or $" % p["id"])
    for i, p in enumerate(pieces):
        for q in pieces[i + 1:]:
            for f in p["files"]:
                for g in q["files"]:
                    if _overlap(f, g):
                        raise ValueError("pieces %s and %s share %s: two pieces never touch the same file"
                                         % (p["id"], q["id"], f))
    return pieces


def create(plan, repo, test=None, model=None, effort="medium", budget=40, max_local=None, account=None):
    """Make the run: save uncommitted work, one worktree + branch per piece, an account per piece.

    With `account`, every piece starts on that one account; a piece still moves to another if it fills up."""
    pieces = validate(plan)
    repo = git(os.path.realpath(repo), "rev-parse", "--show-toplevel")
    rid = time.strftime("%Y%m%d-%H%M%S") + "-" + _uuid.uuid4().hex[:4]
    rows = A.available()
    if account:
        lst = A.load()
        c = A.row(lst, account)
        if c is None:
            raise RuntimeError("unknown account '%s'" % account)
        if not S.can(rows, account):
            raise RuntimeError("'%s' cannot take work now: %s" % (account, A.state(c)[1]))
        order = [account] * len(pieces)
    else:
        order = S.split(len(pieces), rows)
    if not order:
        raise RuntimeError("no account can take work now: " + ", ".join(
            "%s %s" % (c["name"], A.state(c)[1]) for c in A.load()["accounts"]))
    wip = None
    if git(repo, "status", "--porcelain"):
        # the working tree is not a backup: what is uncommitted is saved before anything starts
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "wip: multi-account, saved before run %s" % rid)
        wip = git(repo, "rev-parse", "HEAD")
    base = git(repo, "rev-parse", "HEAD")
    source_branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    os.makedirs(os.path.join(folder(rid), "logs"))
    os.makedirs(reports_folder(rid))
    run = {"id": rid, "task": plan.get("task") or "", "repo": repo, "base": base, "wip": wip,
           "source_branch": source_branch, "test": test or plan.get("test"), "model": model, "effort": effort,
           "budget": budget, "max_local": int(max_local or MAX_LOCAL), "created": now_text(), "state": "working",
           "joined": [], "pieces": []}
    for p, acc in zip(pieces, order):
        wt = os.path.join(WORKTREES, rid, p["id"])
        branch = "ma/%s/%s" % (rid, p["id"])
        os.makedirs(os.path.dirname(wt), exist_ok=True)
        git(repo, "worktree", "add", "-q", "-b", branch, wt, base)
        run["pieces"].append({"id": p["id"], "title": p.get("title") or p["id"], "todo": list(p["todo"]),
                              "files": list(p["files"]), "check": p.get("check"), "kind": "piece",
                              "account": acc, "state": "pending", "worktree": wt, "branch": branch, "pid": None,
                              "failures": 0, "attempts": [], "commit": None, "reason": None})
    save(run)
    note(run, "CREATED %d pieces: %s" % (len(pieces), ", ".join("%s->%s" % (p["id"], p["account"])
                                                                for p in run["pieces"])))
    return run


# ---------- one worker ----------

def reports_folder(rid):
    return os.path.join(WORKTREES, rid, "_reports")


def report(run, p, n=None):
    n = n if n is not None else len(p["attempts"])
    return os.path.join(reports_folder(run["id"]), "%s.%d.md" % (p["id"], n))


def prompt(run, p, previous=None):
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    todo = "\n".join("%s. %s" % (letters[i] if i < 26 else i + 1, t) for i, t in enumerate(p["todo"]))
    rep = report(run, p, len(p["attempts"]) + 1)
    wt = p["worktree"]
    parts = []
    if previous:
        parts.append(
            "A previous worker on another account stopped (%s). Its work is already in your worktree. First run "
            "`git -C %s log --oneline %s..HEAD` and `git -C %s status`, read its report at %s if it exists, keep every "
            "change (never discard anything), and do only what is still PENDING.\n" % (
                previous["reason"], wt, run["base"][:12], wt, previous["report"]))
    if p.get("kind") == "join":
        parts.append(
            "You join one piece of a multi-account run into the joined branch. Run `git -C %s merge --no-ff %s`, "
            "resolve every conflict keeping what BOTH sides meant (read each side's commits first), and commit the "
            "merge with `git -C %s commit --no-edit`. Never drop either side's work.\n" % (wt, p["of_branch"], wt))
    check = ("Your quick check (the only other command you may run): `%s`.\n" % p["check"]) if p.get("check") \
        else "You have no test command: check your work by reading it back.\n"
    parts.append(
        "You are one worker of a multi-account run: several Claude Code workers, each on its own Claude account, build "
        "one task at the same time; each one owns a separate piece in a separate git worktree.\n\n"
        "The whole task, in the owner's words: %s\n\nYour piece: %s - %s\n\nTODO (in order; commit after each item):\n"
        "%s\n\n"
        "Your worktree (work ONLY here, always with absolute paths): %s\n"
        "Your branch: %s (already checked out: never switch branches, never push, never run git reset, checkout, "
        "restore or clean)\n"
        "Files you may create or change (no other worker touches them): %s\n"
        "Every other file belongs to another worker: never edit it. If your piece truly needs a change outside your "
        "files, write it under NEEDS in your report instead.\n\n"
        "Run git only as `git -C %s ...`. Commit after each TODO item: `git -C %s add <your files>` then "
        "`git -C %s commit -m \"%s: <item>\"`.\n%s\n"
        "When you finish, or if you must stop, write your report to %s (an absolute path outside the worktree):\n"
        "RESULT: DONE   (or RESULT: PARTIAL)\n"
        "- A: DONE <commit sha> <how you checked it>\n"
        "- B: PENDING <the exact next step>\n"
        "NEEDS: <changes outside your files, or none>\n\n"
        "Reply with one line: RESULT and the last commit sha.\n\n"
        "MA-REPORT: %s\nMA-FILES: %s\nMA-BRANCH: %s\n" % (
            run["task"], p["id"], p["title"], todo, wt, p["branch"], ", ".join(p["files"]), wt, wt, wt, p["id"],
            check, rep, rep, ",".join(p["files"]), p["branch"]))
    return "\n".join(parts)


def permissions(run, p):
    """The fence of one worker: edits auto-accepted inside its worktree and report folder, git only in its own
    worktree, its piece's check, and nothing else (a headless worker cannot be asked, so the rest is refused)."""
    allowed = TOOLS + ["Bash(git -C %s:*)" % p["worktree"]]
    if p.get("check"):
        allowed.append("Bash(%s)" % p["check"])
    return ["--permission-mode", "acceptEdits", "--add-dir", reports_folder(run["id"]), "--allowedTools"] + allowed


def environment(account_dir):
    env = {k: v for k, v in os.environ.items() if not ENV_OUT.match(k)}
    env.pop("CLAUDE_CONFIG_DIR", None)
    cfg = A.config_env(account_dir)
    if cfg:
        env["CLAUDE_CONFIG_DIR"] = cfg
    env["MULTI_ACCOUNT_WORKER"] = "1"  # lets the user's own hooks tell a worker from an interactive session
    return env


_CHILDREN = {}  # pid -> Popen, for workers this process started (no zombies)


def launch(run, p, name, previous=None):
    c = A.row(A.load(), name)
    text = prompt(run, p, previous)
    n = len(p["attempts"]) + 1
    logs = os.path.join(folder(run["id"]), "logs")
    with open(os.path.join(logs, "%s.%d.prompt.md" % (p["id"], n)), "w") as f:
        f.write(text)
    cmd = ["nice", "-n", "19", CLAUDE, "-p", "--output-format", "json", "--max-budget-usd", str(run["budget"])]
    cmd += permissions(run, p)
    if run.get("model"):
        cmd += ["--model", run["model"]]
    if run.get("effort"):
        cmd += ["--effort", run["effort"]]
    out = open(os.path.join(logs, "%s.%d.json" % (p["id"], n)), "w")
    err = open(os.path.join(logs, "%s.%d.err" % (p["id"], n)), "w")
    proc = subprocess.Popen(cmd + ["--", text], cwd=p["worktree"], env=environment(c["dir"]), stdout=out, stderr=err,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    _CHILDREN[proc.pid] = proc
    with open(os.path.join(folder(run["id"]), "pids.txt"), "a") as f:
        f.write("%d multi-account %s %s account=%s %s\n" % (proc.pid, run["id"], p["id"], name, now_text()))
    p.update(state="working", account=name, pid=proc.pid)
    p["attempts"].append({"account": name, "start": time.time(), "end": None, "exit": None, "reason": None,
                          "pid": proc.pid, "report": report(run, p, n)})
    note(run, "LAUNCH %s on %s (pid %d, attempt %d)" % (p["id"], name, proc.pid, n))


def alive(pid):
    proc = _CHILDREN.get(pid)
    if proc is not None:
        return proc.poll() is None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(st) and not st.startswith("Z")


def _output(run, p):
    """(text of the worker's answer + error tail, exit code or None)."""
    n = len(p["attempts"])
    logs = os.path.join(folder(run["id"]), "logs")
    text, code = "", None
    try:
        raw = open(os.path.join(logs, "%s.%d.json" % (p["id"], n))).read()
        try:
            d = json.loads(raw)
            text = str(d.get("result") or "") + (" [error %s]" % d.get("subtype") if d.get("is_error") else "")
        except ValueError:
            text = raw[-2000:]
    except OSError:
        pass
    try:
        text += "\n" + open(os.path.join(logs, "%s.%d.err" % (p["id"], n))).read()[-2000:]
    except OSError:
        pass
    proc = _CHILDREN.pop(p["pid"], None)
    if proc is not None:
        code = proc.returncode
    return text.strip(), code


def _done(run, p, rep):
    if not re.search(r"^\s*RESULT:\s*DONE\b", rep, re.M):
        return False, "its report does not say RESULT: DONE"
    if p.get("kind") == "join":
        r = git(run["repo"], "merge-base", "--is-ancestor", p["of_branch"], p["branch"], check=False)
        return (r.returncode == 0, "the merge did not land on the joined branch" if r.returncode else "")
    return True, ""


def review(run, p):
    """A worker ended: decide done, relay or failure."""
    text, code = _output(run, p)
    at = p["attempts"][-1]
    at.update(end=time.time(), exit=code)
    try:
        rep = open(at["report"]).read()
    except OSError:
        rep = text  # no report file: the worker's final answer carries its RESULT line
        at["report_in_answer"] = bool(rep)
    p["commit"] = git(run["repo"], "rev-parse", p["branch"])
    ok, why = _done(run, p, rep)
    if LIMIT_RE.search(text) and not ok:
        until = A.mark_full(p["account"], text)
        at["reason"] = "account %s reached its usage limit (full until %s)" % (p["account"], A.hour(until))
        p.update(state="relay", reason=at["reason"])
        note(run, "LIMIT %s on %s: full until %s, moves to another account" % (p["id"], p["account"], A.hour(until)))
        return
    if ok:
        commits = git(run["repo"], "rev-list", "--count", "%s..%s" % (run["base"], p["branch"]))
        p.update(state="done", reason=None if int(commits) else "no changes")
        note(run, "DONE %s on %s (%s commits, %s)" % (p["id"], p["account"], commits, p["commit"][:8]))
        return
    p["failures"] += 1
    at["reason"] = why + ((": " + text[-160:]) if text else ": ended with no answer")
    if p["failures"] >= 2:
        p.update(state="failed", reason=at["reason"])
        note(run, "FAILED %s: %s" % (p["id"], at["reason"]))
    else:
        p.update(state="relay", reason=at["reason"])
        note(run, "RETRY %s on another account: %s" % (p["id"], at["reason"]))


def _load_by_account(run):
    n = {}
    for p in run["pieces"]:
        if p["state"] == "working":
            n[p["account"]] = n.get(p["account"], 0) + 1
    return n


def _account_for(run, p, rows):
    """The account this piece starts on now, or None if none can take it yet."""
    if p["state"] == "pending" and S.can(rows, p["account"]):
        return p["account"]
    other = S.relay(rows, exclude={p["account"]}, load=_load_by_account(run))
    if other:
        return other
    if S.can(rows, p["account"]):
        return p["account"]  # a plain failure with no other account left: the same account tries once more
    return None


def stop(run):
    """Stop only the workers this run started (their PIDs are in its pids.txt)."""
    for p in run["pieces"]:
        if p["state"] == "working" and p.get("pid") and alive(p["pid"]):
            try:
                os.killpg(p["pid"], signal.SIGTERM)  # start_new_session: the worker leads its own group
            except (ProcessLookupError, PermissionError):
                pass
        if p["state"] not in ("done", "failed"):
            p.update(state="stopped", reason="stopped by the owner")
    run["state"] = "stopped"
    note(run, "STOPPED by the owner")


WAITING = "waiting for a ready account"


def step(run):
    """One pass of the watcher. Returns True while the run is still going."""
    import join as J
    if os.path.exists(os.path.join(folder(run["id"]), "STOP")):
        stop(run)
        return False
    for p in run["pieces"]:
        if p["state"] == "working" and not alive(p["pid"]):
            review(run, p)
    rows = A.available()
    live = sum(1 for p in run["pieces"] if p["state"] == "working")
    for p in run["pieces"]:
        if live >= run["max_local"]:
            break
        if p["state"] not in ("pending", "relay"):
            continue
        name = _account_for(run, p, rows)
        if name is None:
            if p.get("reason") != WAITING:
                p["reason"] = WAITING
                note(run, "WAIT %s: no ready account" % p["id"])
            continue
        previous = p["attempts"][-1] if p["attempts"] else None
        launch(run, p, name, previous)
        live += 1
    if any(p["state"] in ("pending", "relay", "working") for p in run["pieces"]):
        return True
    r = J.join(run)
    if r == "waiting-for-join":
        return True
    run["state"] = r
    J.write_report(run)
    note(run, "FINISHED: %s" % r)
    return False


def watch(rid, once=False):
    """The watcher loop: one per run (a lock), until every piece is done, failed or stopped, and joined."""
    lk = open(os.path.join(folder(rid), "watcher.lock"), "a")
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("run %s already has a watcher" % rid)
        return 0
    while True:
        run = load(rid)
        if run["state"] != "working":
            return 0
        going = step(run)
        save(run)
        if not going or once:
            return 0
        time.sleep(PAUSE_S)


def start_watcher(run):
    """Start the watcher detached, so the run goes on even if this session closes; its PID is recorded."""
    log = open(os.path.join(folder(run["id"]), "watcher.out"), "a")
    proc = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "ma.py"),
                             "watch", run["id"]], stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                            start_new_session=True)
    with open(os.path.join(folder(run["id"]), "pids.txt"), "a") as f:
        f.write("%d multi-account-watcher %s %s\n" % (proc.pid, run["id"], now_text()))
    return proc.pid
