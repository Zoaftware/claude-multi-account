"""Join a run's branches, never in the owner's folder.

Every done piece is merged, in plan order, into ma/<id>/joined inside its own worktree. A conflict is aborted at once
and becomes a join piece ("fix-<piece>") for a ready account; the join goes on when it is done. Then the project's
test runs there (--test). With an audit stage, an auditor reads the joined branch; what it finds becomes a fix piece
("correct-<n>") for a developer account, then another audit round, at most AUDIT_ROUNDS audits. The owner's branch only
moves forward (fast-forward) when the test passed, the audit passed (if there was one), no piece failed, and nobody
moved that branch or left uncommitted work in it since the run started; the old point is written in the report so it
can be undone.
"""
import os
import subprocess
import time

import roles as R
import workers as W

TEST_MAX_S = 30 * 60
AUDIT_ROUNDS = int(os.environ.get("MA_AUDIT_ROUNDS") or 3)  # audits per run: up to 2 fixes in between


def _joined_worktree(run):
    wt = os.path.join(W.WORKTREES, run["id"], "_joined")
    branch = "ma/%s/joined" % run["id"]
    if not os.path.isdir(wt):
        W.git(run["repo"], "worktree", "add", "-q", "-b", branch, wt, run["base"])
    return wt, branch


def _fix_piece(run, p):
    return next((q for q in run["pieces"] if q.get("kind") == "join" and q.get("of") == p["id"]), None)


def join(run):
    """'waiting-for-join' while a join piece works, 'waiting-for-audit' while an auditor or a fixer works; else the
    run's final state."""
    wt, branch = _joined_worktree(run)
    run["joined_branch"] = {"worktree": wt, "branch": branch}
    for p in run["pieces"]:
        if p.get("kind") != "piece" or p["state"] != "done" or p["id"] in run["joined"]:
            continue
        fix = _fix_piece(run, p)
        if fix is not None:
            if fix["state"] in ("pending", "relay", "working"):
                return "waiting-for-join"
            if fix["state"] == "done":
                run["joined"].append(p["id"])
                continue
            run.setdefault("not_joined", []).append(p["id"])  # its join piece failed: left out, said in the report
            run["joined"].append(p["id"])
            continue
        r = W.git(wt, "merge", "--no-ff", "--no-edit", "-m", "multi-account %s: join piece %s" % (run["id"], p["id"]),
                  p["branch"], check=False)
        if r.returncode == 0:
            run["joined"].append(p["id"])
            W.note(run, "JOINED %s" % p["id"])
            continue
        conflicts = W.git(wt, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
        W.git(wt, "merge", "--abort", check=False)
        account = W.new_account(run, "dev") or p["account"]
        run["pieces"].append({
            "id": "fix-" + p["id"], "title": "join %s without losing anything" % p["title"], "kind": "join",
            "stage": "dev", "of": p["id"], "of_branch": p["branch"], "todo": [
                "Merge %s into the joined branch and resolve the conflicts in: %s" % (p["branch"], ", ".join(conflicts)),
                "Commit the merge"],
            "files": conflicts or ["."], "check": None, "account": account, "state": "pending", "worktree": wt,
            "branch": branch, "pid": None, "failures": 0, "attempts": [], "commit": None, "reason": None})
        W.note(run, "CONFLICT joining %s (%s): join piece for %s" % (p["id"], ", ".join(conflicts), account))
        return "waiting-for-join"
    head = W.git(wt, "rev-parse", "HEAD")
    run["joined_branch"]["commit"] = head
    failed = [p["id"] for p in run["pieces"] if p["state"] in ("failed", "stopped")] + run.get("not_joined", [])
    if run.get("test_head") != head:  # the test runs again only when a fix changed the joined branch
        run["test_result"] = _test(run, wt)
        run["test_head"] = head
    if "audit" in run.get("stages", []) and not failed and _audit(run, wt, branch, head):
        return "waiting-for-audit"
    run["advance"] = _advance(run, branch, failed)
    if failed:
        return "finished-with-failures"
    tr = run["test_result"]
    if tr and tr["code"] != 0:
        return "test-failed"
    if run.get("audit") == "failed":
        return "audit-failed"
    return "ready" if tr else "ready-untested"


def _audit(run, wt, branch, head):
    """One step of the audit loop. True while an auditor or a fixer has work to do; else run["audit"] is set to
    "passed" (the last audit passed) or "failed" (it still failed after AUDIT_ROUNDS audits)."""
    audits = [p for p in run["pieces"] if p.get("stage") == "audit"]
    if not audits:
        return _new_audit(run, wt, 1)
    last = audits[-1]
    if last.get("verdict") == "PASS":
        run["audit"] = "passed"
        return False
    n = last["round"]
    if not any(p.get("kind") == "correction" and p.get("round") == n for p in run["pieces"]):
        if n >= AUDIT_ROUNDS:
            run["audit"] = "failed"
            W.note(run, "AUDIT not passed after %d rounds" % n)
            return False
        return _new_correction(run, wt, branch, head, last)
    return _new_audit(run, wt, n + 1)


def _new_audit(run, wt, n):
    p = W.role_piece(run, "audit", W.new_account(run, "audit"), round=n, reads=wt)
    run["pieces"].append(p)
    run["stage"] = "audit"
    W.note(run, "AUDIT round %d for %s" % (n, p["account"]))
    return True


def _new_correction(run, wt, branch, head, audit):
    """A developer fixes, on the joined branch itself, what the auditor of round n found."""
    found = audit.get("findings") or []
    todo = list(found) or ["Read the auditor's report at %s and fix every problem it found"
                           % audit["attempts"][-1]["report"]]
    files = sorted({h.split("|")[0].strip() for h in found if "|" in h})
    files = [f for f in files if f and not f.startswith("/") and ".." not in f.split("/")] or ["."]
    p = {"id": "correct-%d" % audit["round"], "title": "fix what audit %d found" % audit["round"],
         "kind": "correction", "stage": "dev", "round": audit["round"], "todo": todo, "files": files, "check": None,
         "account": W.new_account(run, "dev"), "state": "pending", "worktree": wt, "branch": branch, "since": head,
         "pid": None, "failures": 0, "attempts": [], "commit": None, "reason": None}
    run["pieces"].append(p)
    W.note(run, "CORRECTION round %d for %s: %d findings" % (audit["round"], p["account"], len(found)))
    return True


def _test(run, wt):
    if not run.get("test"):
        return None
    t0 = time.time()
    try:
        r = subprocess.run(["nice", "-n", "19", "sh", "-c", run["test"]], cwd=wt, capture_output=True, text=True,
                           timeout=TEST_MAX_S)
        code, out = r.returncode, (r.stdout + r.stderr)[-1500:]
    except subprocess.TimeoutExpired:
        code, out = 124, "the test ran past %d minutes" % (TEST_MAX_S // 60)
    W.note(run, "TEST %s -> %d" % (run["test"], code))
    return {"command": run["test"], "code": code, "output": out, "seconds": round(time.time() - t0)}


def _advance(run, branch, failed):
    repo = run["repo"]
    tr = run.get("test_result")
    if failed:
        return {"done": False, "reason": "some pieces did not finish"}
    if "audit" in run.get("stages", []) and run.get("audit") != "passed":
        return {"done": False, "reason": "the audit did not pass"}
    if not tr:
        return {"done": False, "reason": "the project has no test: the joined branch is ready for review"}
    if tr["code"] != 0:
        return {"done": False, "reason": "the project's test failed"}
    current = W.git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if current != run["source_branch"] or current == "HEAD":
        return {"done": False, "reason": "your folder is no longer on branch %s" % run["source_branch"]}
    if W.git(repo, "rev-parse", "HEAD") != run["base"]:
        return {"done": False, "reason": "your branch %s moved in the meantime" % current}
    if W.git(repo, "status", "--porcelain"):
        return {"done": False, "reason": "your folder has uncommitted changes"}
    r = W.git(repo, "merge", "--ff-only", "-q", branch, check=False)
    if r.returncode != 0:
        return {"done": False, "reason": "git could not fast-forward: " + (r.stderr or r.stdout).strip()[:200]}
    W.note(run, "ADVANCE %s from %s to %s" % (current, run["base"][:8], W.git(repo, "rev-parse", "HEAD")[:8]))
    return {"done": True, "before": run["base"], "after": W.git(repo, "rev-parse", "HEAD"),
            "undo": "git -C %s reset --keep %s" % (repo, run["base"])}


STATES = {"pending": "queued", "working": "working", "relay": "moving to another account", "done": "done",
          "failed": "failed", "stopped": "stopped"}
FINALS = {"ready": "Ready: everything joined and the test passed.",
          "ready-untested": "Everything joined (the project has no test).",
          "test-failed": "Everything joined, but the project's test failed.",
          "audit-failed": "Everything joined, but the audit found problems that could not be fixed.",
          "finished-with-failures": "Finished with pieces that failed.",
          "stopped": "Stopped by you.", "working": "Working."}


def write_report(run):
    """REPORT.md: what the owner reads."""
    lines = ["# Run %s" % run["id"], "", "**%s**" % FINALS.get(run["state"], run["state"]), "",
             "Task: %s" % (run.get("task") or "-"), "", "Stages: %s" % R.chain(run.get("stages") or ["dev"])]
    if run.get("roles"):
        lines.append("Roles: %s" % ", ".join("%s %s" % (n, R.ONLY[r]) for n, r in sorted(run["roles"].items())))
    lines += ["", "| Stage | Piece | Account | State | Commit | Note |", "|---|---|---|---|---|---|"]
    for p in run["pieces"]:
        accounts = " -> ".join(dict.fromkeys(a["account"] for a in p["attempts"])) or p["account"] or "-"
        lines.append("| %s | %s | %s | %s | %s | %s |" % (
            R.NAME[R.stage_of(p)], p["title"], accounts, STATES.get(p["state"], p["state"]),
            (p.get("commit") or "")[:8], p.get("reason") or ""))
    plan = next((p for p in run["pieces"] if p.get("stage") == "plan" and p["state"] == "done"), None)
    if plan:
        lines += ["", "Plan: %s" % plan["output"]]
    if run.get("design"):
        lines += ["", "Design: %s" % run["design"]]
    for p in run["pieces"]:
        if p.get("stage") == "audit" and p.get("verdict"):
            lines += ["", "Audit, round %s: %s" % (p["round"], "passed" if p["verdict"] == "PASS" else
                                                   "found %d problems" % len(p.get("findings") or []))]
            lines += ["- %s" % h for h in p.get("findings") or []]
    tr = run.get("test_result")
    if tr:
        lines += ["", "Test `%s`: %s (%d s)" % (tr["command"], "passed" if tr["code"] == 0 else
                                               "failed, exit code %d" % tr["code"], tr["seconds"]),
                  "", "```", tr["output"].strip()[-800:], "```"]
    adv = run.get("advance") or {}
    jb = run.get("joined_branch") or {}
    if adv.get("done"):
        lines += ["", "Your branch **%s** moved forward to %s. To undo: `%s`" % (run["source_branch"],
                                                                               adv["after"][:8], adv["undo"])]
    elif jb:
        lines += ["", "The joined branch **%s** is in %s. Your branch was not touched: %s." % (
            jb.get("branch"), jb.get("worktree"), adv.get("reason", "-"))]
    with open(os.path.join(W.folder(run["id"]), "REPORT.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    return "\n".join(lines)
