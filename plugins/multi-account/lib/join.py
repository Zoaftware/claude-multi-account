"""Join a run's branches, never in the owner's folder.

Every done piece is merged, in plan order, into ma/<id>/joined inside its own worktree. A conflict is aborted at once
and becomes a join piece ("fix-<piece>") for a ready account; the join goes on when it is done. Then the project's
test runs there (--test). The owner's branch only moves forward (fast-forward) when the test passed, no piece failed,
and nobody moved that branch or left uncommitted work in it since the run started; the old point is written in the
report so it can be undone.
"""
import os
import subprocess
import time

import accounts as A
import split as S
import workers as W

TEST_MAX_S = 30 * 60


def _joined_worktree(run):
    wt = os.path.join(W.WORKTREES, run["id"], "_joined")
    branch = "ma/%s/joined" % run["id"]
    if not os.path.isdir(wt):
        W.git(run["repo"], "worktree", "add", "-q", "-b", branch, wt, run["base"])
    return wt, branch


def _fix_piece(run, p):
    return next((q for q in run["pieces"] if q.get("kind") == "join" and q.get("of") == p["id"]), None)


def join(run):
    """'waiting-for-join' while a join piece works; else the run's final state."""
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
        account = S.relay(A.available()) or p["account"]
        run["pieces"].append({
            "id": "fix-" + p["id"], "title": "join %s without losing anything" % p["title"], "kind": "join",
            "of": p["id"], "of_branch": p["branch"], "todo": [
                "Merge %s into the joined branch and resolve the conflicts in: %s" % (p["branch"], ", ".join(conflicts)),
                "Commit the merge"],
            "files": conflicts or ["."], "check": None, "account": account, "state": "pending", "worktree": wt,
            "branch": branch, "pid": None, "failures": 0, "attempts": [], "commit": None, "reason": None})
        W.note(run, "CONFLICT joining %s (%s): join piece for %s" % (p["id"], ", ".join(conflicts), account))
        return "waiting-for-join"
    run["joined_branch"]["commit"] = W.git(wt, "rev-parse", "HEAD")
    failed = [p["id"] for p in run["pieces"] if p["state"] in ("failed", "stopped")] + run.get("not_joined", [])
    run["test_result"] = _test(run, wt)
    run["advance"] = _advance(run, branch, failed)
    if failed:
        return "finished-with-failures"
    tr = run["test_result"]
    if tr and tr["code"] != 0:
        return "test-failed"
    return "ready" if tr else "ready-untested"


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
          "finished-with-failures": "Finished with pieces that failed.",
          "stopped": "Stopped by you.", "working": "Working."}


def write_report(run):
    """REPORT.md: what the owner reads."""
    lines = ["# Run %s" % run["id"], "", "**%s**" % FINALS.get(run["state"], run["state"]), "",
             "Task: %s" % (run.get("task") or "-"), "", "| Piece | Account | State | Commit | Note |",
             "|---|---|---|---|---|"]
    for p in run["pieces"]:
        accounts = " -> ".join(dict.fromkeys(a["account"] for a in p["attempts"])) or p["account"]
        lines.append("| %s | %s | %s | %s | %s |" % (p["title"], accounts, STATES.get(p["state"], p["state"]),
                                                    (p.get("commit") or "")[:8], p.get("reason") or ""))
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
