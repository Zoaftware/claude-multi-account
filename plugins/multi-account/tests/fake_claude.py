#!/usr/bin/env python3
"""A fake `claude -p` for the tests. It reads the MA-REPORT / MA-FILES / MA-BRANCH lines of the prompt and behaves
as <home>/script.json says, looked up as "piece:<id>", then the account ("main" or the CLAUDE_CONFIG_DIR folder name),
then "commit":
  commit     write its files, commit, report RESULT: DONE
  limit      answer with the usage-limit message and exit 1
  nothing    exit 1 with no answer and no report
  outside    also write shared.txt (outside its files): two of these make a merge conflict
  sleep      sleep 60 s (to test stopping a run)
  no-report  commit, but no report file: only the final answer says RESULT: DONE
Every call appends one JSON line to <home>/ledger.jsonl: account, piece, mode, argv, which planted parent variables
leaked. `claude auth status --json` answers from the folder's test file .fake-auth.json ({"email": ...}) and is not
written to the ledger."""
import json
import os
import re
import subprocess
import sys
import time

# variables the tests plant in the parent; none of them may reach a `claude` run
PROBES = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT", "ANTHROPIC_MODEL", "MA_UNLISTED")
cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
home = os.path.expanduser("~")
if sys.argv[1:3] == ["auth", "status"]:
    folder = cfg or os.path.join(home, ".claude")
    try:
        email = json.load(open(os.path.join(folder, ".fake-auth.json")))["email"]
        print(json.dumps({"loggedIn": True, "email": email}))
    except (OSError, ValueError, KeyError):
        print(json.dumps({"loggedIn": False}))
    sys.exit(0)
prompt = sys.argv[-1]
account = os.path.basename(cfg).replace(".claude-", "") if cfg else "main"
field = lambda k: (re.findall(r"^%s: (.*)$" % k, prompt, re.M) or [""])[-1].strip()
rep, files, branch = field("MA-REPORT"), [f for f in field("MA-FILES").split(",") if f], field("MA-BRANCH")
piece = re.findall(r"^Your piece: (\S+)", prompt, re.M)[0]
script = json.load(open(os.path.join(home, "script.json"))) if os.path.exists(os.path.join(home, "script.json")) else {}
mode = script.get("piece:" + piece) or script.get(account) or "commit"
join = "You join one piece" in prompt
with open(os.path.join(home, "ledger.jsonl"), "a") as f:
    f.write(json.dumps({"account": account, "piece": piece, "mode": "join" if join else mode, "argv": sys.argv[1:-1],
                        "leak": [k for k in PROBES if k in os.environ],
                        "cwd": os.getcwd()}) + "\n")
git = lambda *a: subprocess.run(["git"] + list(a), capture_output=True, text=True)

if join:
    of = re.findall(r"merge --no-ff (\S+?)`", prompt)[0]
    git("merge", "--no-ff", "--no-edit", of)
    for f in git("diff", "--name-only", "--diff-filter=U").stdout.split():
        open(f, "w").write("resolved by %s\n" % account)
        git("add", f)
    git("commit", "--no-edit", "-q")
    open(rep, "w").write("RESULT: DONE\n- A: DONE merged\n")
    print(json.dumps({"result": "RESULT: DONE", "is_error": False}))
    sys.exit(0)
if mode == "limit":
    print(json.dumps({"result": "You've hit your session limit · resets 5pm", "is_error": True}))
    sys.exit(1)
if mode == "nothing":
    sys.exit(1)
if mode == "sleep":
    time.sleep(60)
    sys.exit(0)
for f in files + (["shared.txt"] if mode == "outside" else []):
    os.makedirs(os.path.dirname(f) or ".", exist_ok=True)
    open(f, "w").write("%s done by %s\n" % (piece, account))
    git("add", f)
git("commit", "-q", "-m", "%s: done" % piece)
sha = git("rev-parse", "HEAD").stdout.strip()
if mode != "no-report":  # a worker that may not write its report answers with its RESULT line
    open(rep, "w").write("RESULT: DONE\n- A: DONE %s\nNEEDS: none\n" % sha)
print(json.dumps({"result": "RESULT: DONE %s" % sha, "is_error": False}))
