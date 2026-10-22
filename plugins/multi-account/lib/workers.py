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

Stages (roles.py): a run can plan first, then design, then develop, then audit, each stage on the accounts whose role
names it (or the accounts with no role). A planner, designer or auditor changes no project file: it reads the project
(the _base worktree, or the joined one), writes only its own output in the report folder, and runs only read-only git.
The developer pieces are made when development starts, from the planner's plan.

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
import roles as R
import split as S

RUNS = os.path.join(A.DATA, "runs")
WORKTREES = os.environ.get("MULTI_ACCOUNT_WORKTREES") or os.path.join(A.DATA, "worktrees")
CLAUDE = A.CLAUDE
MAX_LOCAL = int(os.environ.get("MA_MAX_LOCAL") or 6)  # never more workers at once on this machine
PAUSE_S = float(os.environ.get("MA_PAUSE") or 30)
LIMIT_RE = re.compile(r"hit your (weekly|session|usage|opus|sonnet)? ?limit|usage limit reached|limit reached", re.I)
ID_OK = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
RESERVED = re.compile(r"^(joined|plan|design|audit-\d+|correct-\d+)$")  # ids the run gives its own pieces
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
        if not ID_OK.match(str(p.get("id", ""))) or p["id"] in seen or RESERVED.match(p["id"]):
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


def create(plan, repo, test=None, model=None, effort="medium", budget=40, max_local=None, account=None, roles=None,
           stages=None):
    """Make the run: save uncommitted work, then start its first stage.

    The stages are development plus the ones asked for (`stages`, or "stages" in the plan) and the ones an account's
    role names (saved in accounts.json, or `roles` for this run only: {name: stage | None}). With `account`, every
    piece starts on that one account and roles do not apply; a piece still moves to another account if it fills up."""
    draft = validate(plan) if plan.get("pieces") else []
    extra = stages if stages is not None else plan.get("stages")
    asked = R.stages_of(",".join(extra) if isinstance(extra, list) else extra)
    repo = git(os.path.realpath(repo), "rev-parse", "--show-toplevel")
    rid = time.strftime("%Y%m%d-%H%M%S") + "-" + _uuid.uuid4().hex[:4]
    rows = A.available()
    names = [c["name"] for c in rows]
    jobs = {}
    if not account:
        jobs = R.of_accounts(A.load())
        for name, role in (roles or {}).items():
            if name not in names:
                raise RuntimeError("unknown account '%s', or it is not in the split (paused, signed out or repeated)"
                                   % name)
            jobs[name] = role
        jobs = {n: r for n, r in jobs.items() if r and n in names}
    stage_list = [s for s in R.STAGES if s == "dev" or s in asked or s in jobs.values()]
    if not draft and "plan" not in stage_list:
        validate(plan)  # no pieces and nobody to plan them: says so
    for s in stage_list:
        if names and not R.possible(jobs, s, names):
            raise RuntimeError("nobody can do the %s stage: every account has another role. Give one account the role "
                               "'%s' or leave one with no role." % (R.NAME[s], R.VERB[s]))
    if account:
        c = A.row(A.load(), account)
        if c is None:
            raise RuntimeError("unknown account '%s'" % account)
        if not S.can(rows, account):
            raise RuntimeError("'%s' cannot take work now: %s" % (account, A.state(c)[1]))
    elif not S.ready(R.fit(jobs, stage_list[0], rows)):
        raise RuntimeError("no account%s can take work now: %s" % (
            " that may %s" % R.VERB[stage_list[0]] if jobs else "",
            ", ".join("%s %s" % (c["name"], A.state(c)[1]) for c in A.load()["accounts"])))
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
           "joined": [], "pieces": [], "roles": jobs, "stages": stage_list, "stage": None, "fixed_account": account,
           "draft": draft, "design": None}
    _start(run, stage_list[0], rows)
    save(run)
    note(run, "CREATED stages %s; %s" % (" -> ".join(stage_list), ", ".join("%s->%s" % (p["id"], p["account"])
                                                                            for p in run["pieces"])))
    return run


def _start(run, stage, rows):
    """Start one stage: development makes a worktree + branch + account per piece; the others make their role piece
    (an audit starts from join.py, once everything is joined)."""
    run["stage"] = stage
    if stage == "dev":
        _make_pieces(run, run["draft"], rows)
    else:
        run["pieces"].append(role_piece(run, stage, new_account(run, stage, rows)))
    note(run, "STAGE %s" % stage)


def _make_pieces(run, pieces, rows):
    if run.get("fixed_account"):
        order = [run["fixed_account"]] * len(pieces)
    else:  # no ready account yet: each piece waits for one (step)
        order = S.split(len(pieces), R.fit(run["roles"], "dev", rows)) or [None] * len(pieces)
    for p, acc in zip(pieces, order):
        wt = os.path.join(WORKTREES, run["id"], p["id"])
        branch = "ma/%s/%s" % (run["id"], p["id"])
        os.makedirs(os.path.dirname(wt), exist_ok=True)
        git(run["repo"], "worktree", "add", "-q", "-b", branch, wt, run["base"])
        run["pieces"].append({"id": p["id"], "title": p.get("title") or p["id"], "todo": list(p["todo"]),
                              "files": list(p["files"]), "check": p.get("check"), "kind": "piece", "stage": "dev",
                              "account": acc, "state": "pending", "worktree": wt, "branch": branch, "pid": None,
                              "failures": 0, "attempts": [], "commit": None, "reason": None})


def _base_worktree(run):
    """The project as it was when the run started, for the planner and the designer to read (detached: no branch)."""
    wt = os.path.join(WORKTREES, run["id"], "_base")
    if not os.path.isdir(wt):
        os.makedirs(os.path.dirname(wt), exist_ok=True)
        git(run["repo"], "worktree", "add", "-q", "--detach", wt, run["base"])
    return wt


def role_piece(run, stage, account, round=None, reads=None, **extra):
    """A planner, designer or auditor piece: it reads `reads` and writes only its output and report."""
    rep = reports_folder(run["id"])
    pid, title, output = {"plan": ("plan", "plan the task", os.path.join(rep, "plan.json")),
                          "design": ("design", "design the task", os.path.join(rep, "design.md")),
                          "audit": ("audit-%s" % round, "audit, round %s" % round, None)}[stage]
    p = {"id": pid, "title": title, "kind": "role", "stage": stage, "round": round, "todo": [], "files": [],
         "check": None, "account": account, "state": "pending", "worktree": None, "branch": None,
         "reads": reads or _base_worktree(run), "output": output, "pid": None, "failures": 0, "attempts": [],
         "commit": None, "reason": None}
    p.update(extra)
    p["tree"] = tree_print(p["reads"])
    return p


def new_account(run, stage, rows=None):
    """The account a new piece of this stage starts on: the run's fixed account, else the ready one with the least
    work among those that may take the stage; None if none can now (the piece waits)."""
    if run.get("fixed_account"):
        return run["fixed_account"]
    rows = A.available() if rows is None else rows
    return S.relay(R.fit(run.get("roles") or {}, stage, rows))


def tree_print(wt):
    """HEAD, the changed and new files and their diff: what a read-only worker must leave as it found it."""
    import hashlib
    text = git(wt, "rev-parse", "HEAD") + git(wt, "status", "--porcelain", "--untracked-files=all") + \
        git(wt, "diff", "HEAD")
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ---------- one worker ----------

def reports_folder(rid):
    return os.path.join(WORKTREES, rid, "_reports")


def report(run, p, n=None):
    n = n if n is not None else len(p["attempts"])
    return os.path.join(reports_folder(run["id"]), "%s.%d.md" % (p["id"], n))


def prompt(run, p, previous=None):
    if p.get("kind") == "role":
        return role_prompt(run, p, previous)
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    todo = "\n".join("%s. %s" % (letters[i] if i < 26 else i + 1, t) for i, t in enumerate(p["todo"]))
    if run.get("design"):
        todo += "\n\nThe design every developer follows is at %s: read it first and follow it." % run["design"]
    rep = report(run, p, len(p["attempts"]) + 1)
    wt = p["worktree"]
    parts = []
    if p.get("kind") == "correction":
        parts.append(
            "You fix what the auditor found in the joined work of a multi-account run. The joined branch already holds "
            "every piece: fix each finding in your TODO, keep every other change, and never undo another piece's "
            "work.\n")
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
    if p.get("kind") == "correction":
        files = ("Start with these files: %s. Nobody else works on the joined branch now: change what each fix "
                 "needs and nothing more.\n" % ", ".join(p["files"]))
    else:
        files = ("Files you may create or change (no other worker touches them): %s\nEvery other file belongs to "
                 "another worker: never edit it. If your piece truly needs a change outside your files, write it "
                 "under NEEDS in your report instead.\n" % ", ".join(p["files"]))
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
        "%s\n"
        "Run git only as `git -C %s ...`. Commit after each TODO item: `git -C %s add <your files>` then "
        "`git -C %s commit -m \"%s: <item>\"`.\n%s\n"
        "When you finish, or if you must stop, write your report to %s (an absolute path outside the worktree):\n"
        "RESULT: DONE   (or RESULT: PARTIAL)\n"
        "- A: DONE <commit sha> <how you checked it>\n"
        "- B: PENDING <the exact next step>\n"
        "NEEDS: <changes outside your files, or none>\n\n"
        "Reply with one line: RESULT and the last commit sha.\n\n"
        "MA-REPORT: %s\nMA-FILES: %s\nMA-BRANCH: %s\n" % (
            run["task"], p["id"], p["title"], todo, wt, p["branch"], files, wt, wt, wt, p["id"],
            check, rep, rep, ",".join(p["files"]), p["branch"]))
    return "\n".join(parts)


def _pieces_text(pieces):
    lines = []
    for q in pieces:
        lines.append("- %s: %s (files: %s)" % (q["id"], q.get("title") or q["id"], ", ".join(q["files"])))
        lines += ["    - %s" % t for t in q["todo"]]
    return "\n".join(lines) or "- (none yet)"


def role_prompt(run, p, previous=None):
    """The prompt of a planner, designer or auditor: it reads the project and writes only its output and report."""
    stage, reads = p["stage"], p["reads"]
    rep = report(run, p, len(p["attempts"]) + 1)
    who = {"plan": "planner", "design": "designer", "audit": "auditor"}[stage]
    parts = ["You are the %s of a multi-account run: several Claude Code workers, each on its own Claude account, "
             "build one task at the same time, each one in its own git worktree with its own files.\n" % who]
    if previous:
        parts.append("A previous %s on another account stopped (%s). Read its report at %s if it exists%s, keep what "
                     "is good and finish the job.\n" % (who, previous["reason"], previous["report"],
                                                         " and what it wrote at %s" % p["output"] if p["output"]
                                                         else ""))
    parts.append("The whole task, in the owner's words: %s\n\nYour piece: %s - %s\n" % (run["task"], p["id"],
                                                                                      p["title"]))
    parts.append(
        "The %s, read only (always use absolute paths): %s\nYou may read any file there and run `git -C %s log`, "
        "`git -C %s show`, `git -C %s diff` or `git -C %s status`. Never create, edit or delete a file there, never "
        "commit, and run nothing else.\n" % ("joined work" if stage == "audit" else "project", reads, reads, reads,
                                             reads, reads))
    if stage == "plan":
        if run.get("draft"):
            parts.append("The owner's session left this first draft of the pieces; improve it or replace it:\n%s\n"
                         % _pieces_text(run["draft"]))
        parts.append(
            "Your job: split the task into pieces that separate workers build at the same time. Write the plan as JSON "
            "to %s, in exactly this shape:\n"
            '{"pieces": [{"id": "short-id", "title": "what it builds", "todo": ["one step", "the next step"], '
            '"files": ["path/relative/to/the/project"], "check": "one quick command, or null"}]}\n'
            "Rules:\n"
            "- One piece per part that can truly be built in parallel; as many as the task allows, usually 2 to 6.\n"
            "- Two pieces never share a file or a folder: each file the task touches belongs to exactly one piece. "
            "A folder covers every file inside it.\n"
            "- ids: lowercase letters, digits and dashes, unique.\n"
            "- Paths relative to the project root, never absolute, never \"..\".\n"
            "- \"check\": one simple command with no ; & | ` $ < > that checks that piece quickly from its "
            "worktree, or null.\n"
            "- Each TODO item is one concrete step the worker can commit.\n\n"
            "Then write your report to %s:\nRESULT: DONE\n- A: DONE <how many pieces and why this split>\n"
            % (p["output"], rep))
        marks = "MA-PLAN: %s\n" % p["output"]
    elif stage == "design":
        parts.append(
            "Your job: the design the developers will follow. The pieces they will build:\n%s\n\n"
            "Write the design to %s (Markdown): for every screen or part the task touches, what it shows and where, its "
            "components and their states (empty, loading, error), its words, and the colors, fonts and sizes taken "
            "from the project's own styles; then, per piece, what that developer must follow. You may put mockups "
            "(HTML or SVG) in %s/ and name them in the design.\n\n"
            "Then write your report to %s:\nRESULT: DONE\n- A: DONE <what the design covers>\n"
            % (_pieces_text(run["draft"]), p["output"], os.path.join(reports_folder(run["id"]), "design"), rep))
        marks = "MA-DESIGN: %s\n" % p["output"]
    else:
        built = [q for q in run["pieces"] if q.get("kind") == "piece"]
        tr = run.get("test_result")
        test = ("The project's test `%s` ended with code %d. Its last lines:\n```\n%s\n```\nIf the test failed, "
                "your RESULT is FAIL and your first finding is the test.\n" % (
                    tr["command"], tr["code"], tr["output"].strip()[-1200:]) if tr
                else "There is no project test: judge by reading.\n")
        before = [q for q in run["pieces"] if q.get("stage") == "audit" and q is not p and q.get("findings")]
        earlier = ("Round %d found these and a developer was asked to fix them; check each one is fixed:\n%s\n" % (
            before[-1]["round"], "\n".join("- " + h for h in before[-1]["findings"])) if before else "")
        parts.append(
            "Your job: judge whether the joined work does what the task asked. You only read; a developer fixes what "
            "you find. See every change with `git -C %s diff %s..HEAD` and `git -C %s log --oneline %s..HEAD`.\n\n"
            "The pieces that were built:\n%s\n\n%s%s%s"
            "Check that each piece did what its TODO says, that nothing is missing or half done, that it follows the "
            "design, and that there are no clear bugs, broken references or leftovers. Report only real problems, "
            "never taste.\n\n"
            "Write your report to %s:\nRESULT: PASS   (or RESULT: FAIL)\nFINDINGS:\n"
            "- <file> | <what is wrong> | <how to fix it>\n(With PASS, FINDINGS is \"- none\".)\n" % (
                reads, run["base"][:12], reads, run["base"][:12], _pieces_text(built),
                "The design they had to follow: %s\n" % run["design"] if run.get("design") else "", test, earlier,
                rep))
        marks = "MA-ROUND: %s\nMA-JOINED: %s\n" % (p["round"], reads)
    parts.append("Reply with one line: RESULT.\n\nMA-ROLE: %s\n%sMA-REPORT: %s\n" % (stage, marks, rep))
    return "\n".join(parts)


def permissions(run, p):
    """The fence of one worker: edits auto-accepted inside its worktree and report folder, git only in its own
    worktree, its piece's check, and nothing else (a headless worker cannot be asked, so the rest is refused).
    A planner, designer or auditor runs in the report folder: it may write only there, and run read-only git."""
    if p.get("kind") == "role":
        return ["--permission-mode", "acceptEdits", "--allowedTools", "Read", "Glob", "Grep"] + [
            "Bash(git -C %s %s:*)" % (p["reads"], cmd) for cmd in ("log", "show", "diff", "status")]
    allowed = TOOLS + ["Bash(git -C %s:*)" % p["worktree"]]
    if p.get("check"):
        allowed.append("Bash(%s)" % p["check"])
    return ["--permission-mode", "acceptEdits", "--add-dir", reports_folder(run["id"]), "--allowedTools"] + allowed


def environment(account_dir):
    env = A.clean_env(account_dir)
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
    proc = subprocess.Popen(cmd + ["--", text], cwd=p["worktree"] or reports_folder(run["id"]),
                            env=environment(c["dir"]), stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                            start_new_session=True)
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
    if p.get("kind") == "role" and tree_print(p["reads"]) != p["tree"]:
        p["fatal"] = True  # it only reads: a changed tree is never relayed, the work it read is no longer sure
        return False, "the %s changed files it could only read" % {"plan": "planner", "design": "designer",
                                                                    "audit": "auditor"}[p["stage"]]
    if p.get("stage") == "audit":
        return _audited(p, rep)
    if not re.search(r"^\s*RESULT:\s*DONE\b", rep, re.M):
        return False, "its report does not say RESULT: DONE"
    if p.get("kind") == "join":
        r = git(run["repo"], "merge-base", "--is-ancestor", p["of_branch"], p["branch"], check=False)
        return (r.returncode == 0, "the merge did not land on the joined branch" if r.returncode else "")
    if p.get("stage") == "plan":
        try:
            with open(p["output"]) as f:
                p["plan"] = validate(json.load(f))
        except (OSError, ValueError, AttributeError, TypeError, KeyError) as e:
            return False, "the plan is not usable: %s" % e
    if p.get("stage") == "design" and not (os.path.isfile(p["output"]) and os.path.getsize(p["output"])):
        return False, "it wrote no design"
    return True, ""


def _audited(p, rep):
    m = re.search(r"^\s*RESULT:\s*(PASS|FAIL)\b", rep, re.M)
    if not m:
        return False, "its report says neither RESULT: PASS nor RESULT: FAIL"
    p["verdict"] = m.group(1)
    p["findings"] = findings(rep)
    return True, ""


def findings(rep):
    """The FINDINGS lines of an auditor's report ('- <file> | <what> | <fix>'), without '- none'."""
    m = re.search(r"^\s*FINDINGS:(.*)", rep, re.M | re.S)
    out = []
    for line in (m.group(1).splitlines() if m else []):
        line = line.strip()
        if re.match(r"^[A-Z][A-Z -]*:", line):
            break
        if line[:2] in ("- ", "* "):
            line = line[2:].strip()
            if line and line.lower().strip(" .") not in ("none", "nothing"):
                out.append(line)
    return out


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
    if p.get("branch"):
        p["commit"] = git(run["repo"], "rev-parse", p["branch"])
    ok, why = _done(run, p, rep)
    if p.get("fatal"):
        at["reason"] = why
        p.update(state="failed", reason=why)
        note(run, "FAILED %s: %s" % (p["id"], why))
        return
    if LIMIT_RE.search(text) and not ok:
        until = A.mark_full(p["account"], text)
        at["reason"] = "account %s reached its usage limit (full until %s)" % (p["account"], A.hour(until))
        p.update(state="relay", reason=at["reason"])
        note(run, "LIMIT %s on %s: full until %s, moves to another account" % (p["id"], p["account"], A.hour(until)))
        return
    if ok and p.get("kind") == "role":
        said = {"plan": "%d pieces" % len(p.get("plan") or []), "design": "design ready",
                "audit": "passed" if p.get("verdict") == "PASS" else "found %d problems" % len(
                    p.get("findings") or [])}[p["stage"]]
        p.update(state="done", reason=said)
        note(run, "DONE %s on %s: %s" % (p["id"], p["account"], said))
        return
    if ok:
        commits = git(run["repo"], "rev-list", "--count", "%s..%s" % (p.get("since") or run["base"], p["branch"]))
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
    """The account this piece starts on now, or None if none can take it yet (only accounts that may take its stage)."""
    rows = R.fit(run.get("roles") or {}, R.stage_of(p), rows)
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
    if run.get("stage") in ("plan", "design"):
        return _close_stage(run, rows)
    r = J.join(run)
    if r in ("waiting-for-join", "waiting-for-audit"):
        return True
    run["state"] = r
    J.write_report(run)
    note(run, "FINISHED: %s" % r)
    return False


def _close_stage(run, rows):
    """The planner or the designer ended: its output feeds the next stage, or the run ends if it failed."""
    import join as J
    p = [q for q in run["pieces"] if q.get("stage") == run["stage"]][-1]
    if p["state"] != "done":
        run["state"] = "finished-with-failures"
        J.write_report(run)
        note(run, "FINISHED: the %s stage failed" % R.NAME[run["stage"]])
        return False
    if run["stage"] == "plan":
        run["draft"] = p["plan"]
    else:
        run["design"] = p["output"]
    _start(run, run["stages"][run["stages"].index(run["stage"]) + 1], rows)
    return True


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
