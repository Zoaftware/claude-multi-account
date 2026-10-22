#!/usr/bin/env python3
"""multi-account: connect any number of your own Claude accounts, one at a time, and run one task on all of them.

    ma.py accounts                      every account: email, state, live workers
    ma.py connect [name]                add ONE account (a new empty config folder + a sign-in window); again = check it
    ma.py pause|resume|remove NAME      take an account out of the split, or back in; remove keeps folder and login
    ma.py roles [NAME=ROLE ...] [--clear]
                                        what each account does: plan, design, dev, audit or general
    ma.py split --plan P --repo R [--test CMD] [--model M] [--effort E] [--budget USD] [--max-local N] [--account N]
                [--roles NAME=ROLE ...] [--stages plan,design,audit]
                                        start a run: its stages, worktrees, an account per piece, a detached watcher
    ma.py status [ID]                   the pieces of a run (the last one by default)
    ma.py pane                          every account, the last run and the newest artifacts as JSON, for the pane
    ma.py artifacts [--search T] [--account N] [--n N] [--json]
                                        every account's artifact copies (see artifacts.py), newest first
    ma.py view ID                       open an artifact's local copy in the browser, whatever account owns it
    ma.py gallery [--no-open]           one local page with every account's artifacts, search and a filter per account
    ma.py wait ID                       block until the run ends (for a background shell), then print its report
    ma.py stop ID                       stop a run: only its own workers
    ma.py watch ID [--once]             the watcher loop (split starts it by itself)

Data folder: $MULTI_ACCOUNT_HOME, else ~/.multi-account.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import accounts as A  # noqa: E402
import artifacts as R  # noqa: E402
import join as J  # noqa: E402
import roles as RL  # noqa: E402
import workers as W  # noqa: E402

hour = A.hour


def _live_by_account():
    n = {}
    try:
        ids = os.listdir(W.RUNS)
    except OSError:
        return n
    for rid in ids:
        try:
            run = W.load(rid)
        except Exception:
            continue
        if run.get("state") != "working":
            continue
        for p in run["pieces"]:
            if p["state"] == "working":
                n[p["account"]] = n.get(p["account"], 0) + 1
    return n


def table(rows, head):
    widths = [max(len(str(x)) for x in col) for col in zip(head, *rows)]
    line = lambda r: "  ".join(str(x).ljust(w) for x, w in zip(r, widths)).rstrip()
    return "\n".join([line(head), line(["-" * w for w in widths])] + [line(r) for r in rows])


def accounts_data():
    """Every account as plain data: what the table and the pane show."""
    lst = A.load()
    live = _live_by_account()
    me = A.this_session()
    out = []
    for c in lst["accounts"]:
        key, text = A.state(c)
        until = A.full_until(c["name"]) if key == "full" else None
        out.append({"name": c["name"], "email": None if key == "unknown" else c.get("email"), "dir": c["dir"],
                    "key": key, "text": text, "full_until": until, "working": live.get(c["name"], 0),
                    "role": c.get("role") if c.get("role") in RL.STAGES else None,
                    "this_session": bool(me) and c.get("login") == me and key not in ("unknown", "duplicate"),
                    "login": A.login_command(c["name"]) if key == "not-signed-in" and not A.is_main(c["dir"])
                    else None})
    return out


def cmd_accounts(a):
    accs = accounts_data()
    here = " (this app)" if A.in_app() else " (this session)"
    rows = [[c["name"] + (here if c["this_session"] else ""), c["email"] or "-", c["text"], c["working"] or "-"]
            for c in accs]
    head = ["Account", "Email", "State", "Working"]
    if any(c["role"] for c in accs):  # the column shows only once an account has a role
        for r, c in zip(rows, accs):
            r.insert(2, RL.ONLY[c["role"]] if c["role"] else "any stage")
        head.insert(2, "Role")
    ready = sum(1 for c in accs if c["key"] == "ready")
    print(table(rows, head))
    print()
    print("%d account%s ready to take work." % (ready, "" if ready == 1 else "s"))
    owner = next((c for c in accs if c["this_session"]), None)
    for c in accs:
        if c["key"] == "unknown" and owner:
            print("-> '%s' takes no work here: the Claude app writes its own account (%s) into %s, and '%s' already "
                  "holds that account, so the login a run in %s would use is unknown." % (
                      c["name"], owner["email"], c["dir"], owner["name"], c["dir"]))
    for c in accs:
        if c["login"]:
            print("-> '%s' is not signed in: in a terminal run  %s  and sign in there." % (c["name"], c["login"]))
    return 0


def cmd_connect(a):
    r = A.connect(a.name)
    if r["new"]:
        print("New account '%s': empty config folder %s created." % (r["name"], r["dir"]))
    if r["state"] == "not-signed-in":
        opened = not a.no_open and A.open_terminal(r["name"])
        if opened:
            print("-> A Terminal window is open inside '%s': sign in there with that account's email." % r["name"])
        else:
            print("-> In a terminal run:  %s   then sign in with that account's email." % r["login"])
        print("   When you are done, run 'connect %s' again or look at the table: it shows as ready." % r["name"])
    elif r["state"] == "duplicate":
        print("'%s' has the same login as '%s' (%s): it does not count as another account. Sign in there with a "
              "different account." % (r["name"], r["duplicate_of"], r["email"]))
    elif r["state"] == "unknown":
        print("'%s': %s (the app writes its own account into %s); it takes no work here." % (r["name"], r["text"],
                                                                                            r["dir"]))
    else:
        print("'%s' connected: %s - %s." % (r["name"], r["email"] or "-", r["text"]))
    return 0


def cmd_change(a):
    c = A.change(a.name, a.action)
    print({"pause": "'%s' is paused: it takes no work.",
           "resume": "'%s' is back in the split.",
           "remove": "'%s' left the list (its folder and its login stay, in case you want it back)."}[a.action]
          % c["name"])
    return 0


def cmd_roles(a):
    """What each account does. A role turns its stage on for every task; 'general' takes the role away."""
    if a.clear:
        for c in A.load()["accounts"]:
            if c.get("role"):
                A.set_role(c["name"], None)
        print("No account has a role: every account takes any stage.")
    for name, role in RL.parse(a.pairs).items():
        A.set_role(name, role)
        print("'%s' %s." % (name, "now " + RL.ONLY[role] if role else "takes any stage again"))
    lst = A.load()
    rows = [[c["name"], RL.ONLY[c["role"]] if c.get("role") in RL.STAGES else "any stage",
             "" if c.get("active", True) else "(paused)"] for c in lst["accounts"] if not c.get("duplicate_of")]
    print()
    print(table(rows, ["Account", "Role", ""]))
    jobs = RL.of_accounts(lst)
    names = [c["name"] for c in A.available(lst)]
    stages = [s for s in RL.STAGES if s == "dev" or s in jobs.values()]
    print()
    print("Every task goes through: %s." % RL.chain(stages))
    for s in stages:
        if not RL.possible(jobs, s, names):
            print("-> Nobody can %s: give one account that role or leave one with no role." % RL.VERB[s])
    return 0


def cmd_split(a):
    plan = json.load(open(a.plan))
    roles = RL.parse(a.roles) if a.roles else None
    run = W.create(plan, a.repo, test=a.test, model=a.model, effort=a.effort, budget=a.budget,
                   max_local=a.max_local, account=a.account, roles=roles, stages=a.stages)
    if run.get("wip"):
        print("Saved your uncommitted work in a wip commit (%s) before starting." % run["wip"][:8])
    if run["stage"] == "dev":
        accs = {}
        for p in run["pieces"]:
            accs.setdefault(p["account"] or "waiting for a ready account", []).append(p["title"])
        print("Run %s: %d pieces on %d accounts at once." % (run["id"], len(run["pieces"]), len(accs)))
        for n, ts in accs.items():
            print("   %s: %s" % (n, "; ".join(ts)))
    else:
        p = run["pieces"][-1]
        print("Run %s: starts to %s on %s; the pieces are made when it ends." % (
            run["id"], RL.VERB[run["stage"]], p["account"] or "the first ready account"))
    if len(run["stages"]) > 1:
        print("   Stages: %s" % RL.chain(run["stages"]))
    if run.get("roles"):
        print("   Roles: %s" % ", ".join("%s %s" % (n, RL.ONLY[r]) for n, r in sorted(run["roles"].items())))
    if not a.no_watcher:
        pid = W.start_watcher(run)
        print("The watcher started (pid %d) and checks every %d s." % (pid, W.PAUSE_S))
    print("ID=%s" % run["id"])
    return 0


def run_data(rid):
    """One run as plain data: its pieces, who has each, minutes and commits."""
    run = W.load(rid)
    now = time.time()
    pieces = []
    for p in run["pieces"]:
        at = p["attempts"][-1] if p["attempts"] else None
        try:
            commits = int(W.git(run["repo"], "rev-list", "--count", "%s..%s" % (p.get("since") or run["base"],
                                                                                p["branch"]))) if p.get("branch") else None
        except Exception:
            commits = None
        pieces.append({"id": p["id"], "title": p["title"], "account": p["account"], "state": p["state"],
                       "stage": RL.NAME[RL.stage_of(p)],
                       "state_text": J.STATES.get(p["state"], p["state"]),
                       "minutes": int(((at["end"] or now) - at["start"]) / 60) if at else None,
                       "commits": commits, "reason": p.get("reason") or "",
                       "accounts": list(dict.fromkeys(x["account"] for x in p["attempts"])) or [p["account"]]})
    rep = os.path.join(W.folder(rid), "REPORT.md")
    return {"id": rid, "task": run.get("task") or "", "repo": run["repo"], "state": run["state"],
            "state_text": J.FINALS.get(run["state"], run["state"]), "pieces": pieces,
            "stages": RL.chain(run.get("stages") or ["dev"]), "roles": run.get("roles") or {},
            "stopping": os.path.exists(os.path.join(W.folder(rid), "STOP")),
            "report": rep if run["state"] != "working" and os.path.exists(rep) else None}


def cmd_status(a):
    rid = a.id or W.last()
    if not rid:
        print("No runs yet.")
        return 0
    d = run_data(rid)
    rows = [[p["stage"], p["title"][:40], p["account"] or "-", p["state_text"],
             "%d" % p["minutes"] if p["minutes"] is not None else "-",
             p["commits"] if p["commits"] is not None else "-", p["reason"][:60]] for p in d["pieces"]]
    print("Run %s - %s" % (rid, d["state_text"]))
    print("Stages: %s" % d["stages"])
    print(table(rows, ["Stage", "Piece", "Account", "State", "Min", "Commits", "Note"]))
    if d["report"]:
        print("\nReport: %s" % d["report"])
    return 0


def cmd_pane(a):
    """The pane beside the chat reads this: every account and the last run, as JSON."""
    rid = W.last()
    print(json.dumps({"now": time.time(), "accounts": accounts_data(),
                      "run": run_data(rid) if rid else None, "artifacts": R.summary()}, ensure_ascii=False))
    return 0


def cmd_artifacts(a):
    lst = R.listing(search=a.search, account=a.account)
    if a.json:
        print(json.dumps(lst[:a.n], ensure_ascii=False))
        return 0
    if not lst:
        print("No artifact copies found%s in %s." % (" matching '%s'" % a.search if a.search else "", R.ARTIFACTS))
        return 0
    rows = [[x["title"][:50], x["account"], x["when_text"], "yes" if x["copy"] else "no", x["id"]] for x in lst[:a.n]]
    print(table(rows, ["Artifact", "Account", "Last change", "Copy", "Id"]))
    print()
    print("%d artifact%s in all. 'view <id>' opens the copy without switching accounts; 'gallery' shows them all." % (
        len(lst), "" if len(lst) == 1 else "s"))
    return 0


def cmd_view(a):
    x = R.find_one(a.id)
    if not x["copy"]:
        print("'%s' has no saved page. Open it in Claude with the account %s: %s" % (x["title"], x["account"],
                                                                                   x["url"]))
        return 0
    opened = R.open_in_browser(x["copy"])
    print("%s '%s' (from %s)%s: %s" % ("Opened" if opened else "Open in your browser", x["title"], x["account"],
                                       ", without its live data" if x["live"] else "", x["copy"]))
    return 0


def cmd_gallery(a):
    path, n = R.gallery()
    opened = not a.no_open and R.open_in_browser(path)
    print("%s the page with the %d artifacts of every account: %s" % ("Opened" if opened else "Built", n, path))
    return 0


def cmd_wait(a):
    while W.load(a.id)["state"] == "working":
        time.sleep(W.PAUSE_S)
    rep = os.path.join(W.folder(a.id), "REPORT.md")
    print(open(rep).read() if os.path.exists(rep) else W.load(a.id)["state"])
    return 0


def cmd_stop(a):
    open(os.path.join(W.folder(a.id), "STOP"), "w").close()
    print("Asked run %s to stop: the watcher stops only its own workers on its next pass (at most %d s)."
          % (a.id, W.PAUSE_S))
    return 0


def cmd_watch(a):
    return W.watch(a.id, once=a.once)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ma.py", description="multi-account")
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("accounts")
    s.set_defaults(f=cmd_accounts)
    s = sub.add_parser("connect")
    s.add_argument("name", nargs="?")
    s.add_argument("--no-open", action="store_true")
    s.set_defaults(f=cmd_connect)
    for action in ("pause", "resume", "remove"):
        s = sub.add_parser(action)
        s.add_argument("name")
        s.set_defaults(f=cmd_change, action=action)
    s = sub.add_parser("roles")
    s.add_argument("pairs", nargs="*", help="account=role: plan, design, dev, audit or general")
    s.add_argument("--clear", action="store_true", help="take the role away from every account")
    s.set_defaults(f=cmd_roles)
    s = sub.add_parser("split")
    s.add_argument("--plan", required=True)
    s.add_argument("--repo", required=True)
    s.add_argument("--test")
    s.add_argument("--model")
    s.add_argument("--effort", default="medium")
    s.add_argument("--budget", type=float, default=40)
    s.add_argument("--max-local", type=int)
    s.add_argument("--no-watcher", action="store_true")
    s.add_argument("--account", help="every piece starts on this one account")
    s.add_argument("--roles", nargs="+", help="for this run only: account=role (plan, design, dev, audit or general)")
    s.add_argument("--stages", nargs="+", help="stages on top of development: plan, design, audit")
    s.set_defaults(f=cmd_split)
    s = sub.add_parser("status")
    s.add_argument("id", nargs="?")
    s.set_defaults(f=cmd_status)
    s = sub.add_parser("pane")
    s.set_defaults(f=cmd_pane)
    s = sub.add_parser("artifacts")
    s.add_argument("--search")
    s.add_argument("--account")
    s.add_argument("--n", type=int, default=30)
    s.add_argument("--json", action="store_true")
    s.set_defaults(f=cmd_artifacts)
    s = sub.add_parser("view")
    s.add_argument("id")
    s.set_defaults(f=cmd_view)
    s = sub.add_parser("gallery")
    s.add_argument("--no-open", action="store_true")
    s.set_defaults(f=cmd_gallery)
    for command, f in (("wait", cmd_wait), ("stop", cmd_stop)):
        s = sub.add_parser(command)
        s.add_argument("id")
        s.set_defaults(f=f)
    s = sub.add_parser("watch")
    s.add_argument("id")
    s.add_argument("--once", action="store_true")
    s.set_defaults(f=cmd_watch)
    a = ap.parse_args(argv)
    try:
        return a.f(a)
    except (ValueError, RuntimeError, FileNotFoundError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
