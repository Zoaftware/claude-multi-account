#!/usr/bin/env python3
"""multi-account tests: no AI, no login secrets, no network. Each test builds a fake home (config folders, a git
project) and runs ma.py as the user would; `claude` (sign-in checks and workers) is fake_claude.py next to this
file. They live outside the plugin folder, so an install carries only what runs.

    python3 tests/multi-account/test_multi_account.py -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(os.path.dirname(os.path.dirname(HERE)), "plugins", "multi-account", "lib")
MA = os.path.join(LIB, "ma.py")
FAKE = os.path.join(HERE, "fake_claude.py")
NEVER_MODE = "bypass" + "Permissions"  # the permission mode a worker must never get
ARTIFACT_URL = "urn:artifact:"  # the tests name no web host


class World:
    """A fake home with config folders and a git project."""

    def __init__(self):
        self.home = tempfile.mkdtemp(prefix="ma-test-")
        self.data = os.path.join(self.home, "data")
        os.makedirs(os.path.join(self.home, ".claude"))
        self.script = {}
        self.sign_in("~/.claude", "me@home")
        self.sign_in("~/.claude-work", "work@office")
        self.sign_in("~/.claude-spare", "Me@Home")  # the same login as the main one
        self.repo = os.path.join(self.home, "project")
        os.makedirs(self.repo)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@t")
        self.git("config", "user.name", "t")
        open(os.path.join(self.repo, "README.md"), "w").write("project\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "start")
        self.ledger = os.path.join(self.home, "ledger.jsonl")
        open(self.ledger, "w").close()

    def git(self, *a):
        return subprocess.run(["git", "-C", self.repo] + list(a), capture_output=True, text=True, check=True).stdout

    def sign_in(self, d, email):
        """A sign-in in that folder: the fake `claude auth status` then answers with this email."""
        folder = os.path.join(self.home, d[2:])
        os.makedirs(folder, exist_ok=True)
        json.dump({"email": email}, open(os.path.join(folder, ".fake-auth.json"), "w"))

    def env(self, **extra):
        json.dump(self.script, open(os.path.join(self.home, "script.json"), "w"))
        e = {"PATH": os.environ.get("PATH") or "/usr/bin:/bin", "HOME": self.home, "MA_HOME": self.home,
             "MULTI_ACCOUNT_HOME": self.data, "MA_CLAUDE": FAKE, "MA_PAUSE": "0.2", "MA_NO_OPEN": "1"}
        e.update(extra)
        return e

    def ma(self, *args, **extra):
        return subprocess.run([sys.executable, MA] + list(args), capture_output=True, text=True, env=self.env(**extra),
                              timeout=120)

    def accounts(self):
        return json.load(open(os.path.join(self.data, "accounts.json")))

    def full(self):
        try:
            return json.load(open(os.path.join(self.data, "full.json")))
        except FileNotFoundError:
            return {}

    def plan(self, pieces, **extra):
        f = os.path.join(self.home, "plan.json")
        json.dump(dict({"task": "test", "pieces": pieces}, **extra), open(f, "w"))
        return f

    def start(self, pieces, test=None, max_local=None, account=None, **extra):
        args = ["split", "--plan", self.plan(pieces), "--repo", self.repo, "--no-watcher"]
        if test:
            args += ["--test", test]
        if account:
            args += ["--account", account]
        if max_local:
            args += ["--max-local", str(max_local)]
        r = self.ma(*args, **extra)
        assert r.returncode == 0, r.stderr + r.stdout
        return [l for l in r.stdout.splitlines() if l.startswith("ID=")][0][3:]

    def run(self, pieces, test=None, max_local=None, account=None, **extra):
        rid = self.start(pieces, test, max_local, account, **extra)
        w = self.ma("watch", rid, **extra)
        assert w.returncode == 0, w.stderr + w.stdout
        return rid, self.load(rid)

    def load(self, rid):
        return json.load(open(os.path.join(self.data, "runs", rid, "run.json")))

    def file(self, rid, name):
        return open(os.path.join(self.data, "runs", rid, name)).read()

    def calls(self):
        return [json.loads(l) for l in open(self.ledger) if l.strip()]

    def remove(self):
        shutil.rmtree(self.home, ignore_errors=True)


def piece(pid, files, **extra):
    return dict({"id": pid, "title": "piece " + pid, "todo": ["do " + pid], "files": files}, **extra)


class Lib(unittest.TestCase):
    """The pure parts, imported with a throwaway data folder."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ma-lib-")
        os.environ["MULTI_ACCOUNT_HOME"] = cls.tmp
        os.environ["MA_HOME"] = cls.tmp
        os.environ["MA_CLAUDE"] = FAKE  # never the real `claude`
        sys.path.insert(0, LIB)
        import accounts
        import split
        cls.A, cls.S = accounts, split

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        os.environ.pop("MULTI_ACCOUNT_HOME", None)
        os.environ.pop("MA_HOME", None)
        os.environ.pop("MA_CLAUDE", None)

    def setUp(self):
        try:
            os.remove(self.A.FULL)
        except FileNotFoundError:
            pass

    def rows(self, *names):
        return [{"name": n} for n in names]

    def test_even_split_reaches_every_account_first(self):
        self.assertEqual(self.S.split(3, self.rows("a", "b", "c")), ["a", "b", "c"])
        self.assertEqual(self.S.split(7, self.rows("a", "b", "c")), ["a", "b", "c", "a", "b", "c", "a"])
        self.assertEqual(self.S.split(2, self.rows()), [])

    def test_a_full_account_is_skipped_until_its_reset(self):
        now = time.time()
        self.A.mark_full("b", "You've hit your session limit · resets 5pm", now)
        self.assertEqual(self.S.split(4, self.rows("a", "b", "c")), ["a", "c", "a", "c"])
        self.assertFalse(self.S.can(self.rows("a", "b"), "b"))
        until = self.A.full_until("b")
        self.assertEqual(time.localtime(until)[3:5], (17, 0))
        self.assertTrue(now < until <= now + 86400)
        # once the reset time has passed, the account takes work again
        self.assertIsNone(self.A.full_until("b", now=until + 1))
        self.assertEqual(self.S.split(3, self.rows("a", "b", "c"), now=until + 1), ["a", "b", "c"])
        self.A.mark_full("a", "usage limit reached", now)  # no time in the message: one hour
        self.assertAlmostEqual(self.A.full_until("a", now), now + 3600, delta=2)
        self.assertEqual(self.S.split(1, self.rows("a", "b"), now=now + 1), [])

    def test_a_full_row_says_when_the_limit_was_seen(self):
        now = time.time()
        self.A.mark_full("b", "You've hit your session limit · resets 5pm", now)
        m = self.A.full_mark("b", now)
        self.assertTrue(m["named"])
        self.assertEqual(self.A.full_label(m), "full until %s (limit seen %s)" % (self.A.hour(m["until"]),
                                                                                  self.A.hour(now)))
        self.A.mark_full("a", "usage limit reached", now)  # no time in the message: one hour, and the row says so
        self.assertEqual(self.A.full_label(self.A.full_mark("a", now)),
                         "full until %s (limit seen %s, no reset time in the message: one hour)"
                         % (self.A.hour(now + 3600), self.A.hour(now)))
        # a mark written by an older version (no "named") still reads, without the one-hour note
        self.assertEqual(self.A.full_label({"until": now + 60, "since": now}),
                         "full until %s (limit seen %s)" % (self.A.hour(now + 60), self.A.hour(now)))
        self.assertIsNone(self.A.full_mark("b", now=m["until"] + 1))

    def test_relay_picks_the_least_loaded_ready_account(self):
        rows = self.rows("a", "b", "c")
        self.assertEqual(self.S.relay(rows, exclude={"a"}, load={"b": 2, "c": 1}), "c")
        self.assertEqual(self.S.relay(rows, exclude={"a"}), "b")
        self.assertIsNone(self.S.relay(rows, exclude={"a", "b", "c"}))

    def test_reset_times_read_from_the_message(self):
        now = time.mktime((2026, 10, 21, 14, 0, 0, 0, 0, -1))
        at = lambda ts: time.localtime(ts)[1:5]
        self.assertEqual(at(self.A.reset_from_message("resets 5pm", now)), (10, 21, 17, 0))
        self.assertEqual(at(self.A.reset_from_message("resets 9:30am", now)), (10, 22, 9, 30))
        self.assertEqual(at(self.A.reset_from_message("Limit reached · resets Oct 23, 4am (UTC)", now)),
                         (10, 23, 4, 0))
        self.assertEqual(at(self.A.reset_from_message("resets at 18:45", now)), (10, 21, 18, 45))
        self.assertIsNone(self.A.reset_from_message("usage limit reached", now))


class Accounts(unittest.TestCase):
    def setUp(self):
        self.m = World()

    def tearDown(self):
        self.m.remove()

    def test_imports_and_marks_duplicates(self):
        r = self.m.ma("accounts")
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = {c["name"]: c for c in self.m.accounts()["accounts"]}
        self.assertEqual(sorted(rows), ["main", "spare", "work"])
        self.assertEqual(rows["spare"]["duplicate_of"], "main")
        self.assertIsNone(rows["work"]["duplicate_of"])
        self.assertIn("2 accounts ready", r.stdout)
        self.assertIn("duplicate of main", r.stdout)

    def test_marks_this_app_and_lets_another_folder_own_the_app_account(self):
        app = {"CLAUDE_CODE_USER_EMAIL": "ME@home", "CLAUDE_CODE_DESKTOP_APP_VERSION": "1.0"}
        r = self.m.ma("accounts", **app)
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = {c["name"]: c for c in self.m.accounts()["accounts"]}
        self.assertIsNone(rows["spare"]["duplicate_of"])  # the other folder owns the app's account
        self.assertEqual(rows["main"]["duplicate_of"], "spare")
        self.assertIn("spare (this app)", r.stdout)
        self.assertIn("login unknown in the Claude app", r.stdout)
        self.assertIn("-> 'main' takes no work here", r.stdout)
        self.assertIn("2 accounts ready", r.stdout)
        d = {c["name"]: c for c in json.loads(self.m.ma("pane", **app).stdout)["accounts"]}
        self.assertEqual((d["main"]["key"], d["main"]["email"], d["main"]["this_session"]), ("unknown", None, False))
        self.assertTrue(d["spare"]["this_session"])
        self.m.run([piece("a", ["a.txt"]), piece("b", ["b.txt"]), piece("c", ["c.txt"])], **app)
        calls = self.m.calls()
        self.assertEqual(sorted({l["account"] for l in calls}), ["spare", "work"])  # no piece runs on main
        self.assertEqual([l["leak"] for l in calls if l["leak"]], [])
        # outside the app the session's email only marks its row
        r = self.m.ma("accounts", CLAUDE_CODE_USER_EMAIL="me@home")
        self.assertIn("main (this session)", r.stdout)
        self.assertIn("duplicate of main", r.stdout)
        self.assertNotIn("unknown", r.stdout)
        r = self.m.ma("accounts")
        self.assertNotIn("(this", r.stdout)
        self.assertNotIn("unknown", r.stdout)

    def test_connect_one_at_a_time(self):
        r = self.m.ma("connect")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("'account2'", r.stdout)  # the next free name
        folder = os.path.join(self.m.home, ".claude-account2")
        self.assertEqual(os.listdir(folder), [])  # an empty folder: nothing copied or linked from another account
        self.assertEqual(os.stat(folder).st_mode & 0o777, 0o700)
        self.assertIn('CLAUDE_CONFIG_DIR="$HOME/.claude-account2" claude', r.stdout)
        row = [c for c in self.m.accounts()["accounts"] if c["name"] == "account2"][0]
        self.assertIsNone(row["login"])
        self.assertIn("not signed in", self.m.ma("accounts").stdout)
        self.assertFalse(os.path.exists(os.path.join(self.m.home, ".zshrc")))  # no shell file is edited
        # the user signs in there
        self.m.sign_in("~/.claude-account2", "new@home")
        r = self.m.ma("connect", "account2")
        self.assertIn("connected", r.stdout)
        self.assertIn("3 accounts ready", self.m.ma("accounts").stdout)
        # one more, with a login that is already connected
        self.m.ma("connect", "other")
        self.m.sign_in("~/.claude-other", "work@office")
        r = self.m.ma("connect", "other")
        self.assertIn("same login as 'work'", r.stdout)
        self.assertIn("3 accounts ready", self.m.ma("accounts").stdout)
        self.assertNotEqual(self.m.ma("connect", "all").returncode, 0)  # a route word is not a name

    def test_pause_resume_remove(self):
        self.m.ma("accounts")
        self.assertIn("paused", self.m.ma("pause", "work").stdout)
        self.assertIn("1 account ready", self.m.ma("accounts").stdout)
        self.m.ma("resume", "work")
        self.assertIn("2 accounts ready", self.m.ma("accounts").stdout)
        self.assertNotEqual(self.m.ma("remove", "main").returncode, 0)
        self.m.ma("remove", "work")
        self.assertTrue(os.path.isdir(os.path.join(self.m.home, ".claude-work")))  # folder and login stay
        self.assertNotIn("work", [c["name"] for c in self.m.accounts()["accounts"]])


class Runs(unittest.TestCase):
    def setUp(self):
        self.m = World()
        self.m.ma("connect", "team")
        self.m.sign_in("~/.claude-team", "team@office")

    def tearDown(self):
        self.m.remove()

    def test_three_accounts_at_once_and_your_branch_moves_forward(self):
        base = self.m.git("rev-parse", "HEAD").strip()
        rid, run = self.m.run([piece("a", ["a.txt"]), piece("b", ["src/b.txt"]), piece("c", ["c.txt"])],
                              test="test -f a.txt && test -f src/b.txt && test -f c.txt")
        self.assertEqual(run["state"], "ready", self.m.file(rid, "watcher.log"))
        calls = self.m.calls()
        self.assertEqual(sorted(l["account"] for l in calls), ["main", "team", "work"])
        self.assertEqual([l["leak"] for l in calls], [[]] * 3)
        for l in calls:  # the fence: never skip permissions, git only in its own worktree
            self.assertNotIn(NEVER_MODE, l["argv"])
            self.assertIn("acceptEdits", l["argv"])
            rules = [os.path.realpath(a[len("Bash(git -C "):-len(":*)")]) for a in l["argv"]
                     if a.startswith("Bash(git -C ")]
            self.assertEqual(rules, [os.path.realpath(l["cwd"])])
        self.assertTrue(self.m.git("merge-base", "--is-ancestor", base, "HEAD") == "")
        for f in ("a.txt", "src/b.txt", "c.txt"):
            self.assertTrue(os.path.exists(os.path.join(self.m.repo, f)), f)
        self.assertTrue(run["advance"]["done"])
        self.assertIn("reset --keep %s" % base, run["advance"]["undo"])
        self.assertIn("Ready: everything joined", self.m.file(rid, "REPORT.md"))

    def test_one_account_does_every_piece(self):
        rid, run = self.m.run([piece("a", ["a.txt"]), piece("b", ["b.txt"])], account="team")
        self.assertEqual(run["state"], "ready-untested")
        self.assertEqual([l["account"] for l in self.m.calls()], ["team", "team"])
        plan = self.m.plan([piece("c", ["c.txt"])])
        r = self.m.ma("split", "--plan", plan, "--repo", self.m.repo, "--no-watcher", "--account", "nobody")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unknown account 'nobody'", r.stderr)

    def test_pane_gives_accounts_and_run(self):
        empty = json.loads(self.m.ma("pane").stdout)
        self.assertIsNone(empty["run"])
        rid, run = self.m.run([piece("a", ["a.txt"]), piece("b", ["b.txt"])])
        d = json.loads(self.m.ma("pane").stdout)
        rows = {c["name"]: c for c in d["accounts"]}
        self.assertEqual(rows["team"]["key"], "ready")
        self.assertEqual(rows["spare"]["key"], "duplicate")
        self.assertNotIn("week", rows["team"])  # usage is never measured
        self.assertEqual(d["run"]["id"], rid)
        self.assertEqual(d["run"]["state"], run["state"])
        self.assertEqual([p["id"] for p in d["run"]["pieces"]], ["a", "b"])
        self.assertTrue(all(p["commits"] >= 1 and p["state"] == "done" for p in d["run"]["pieces"]))
        self.assertTrue(d["run"]["report"].endswith("REPORT.md"))

    def test_limit_message_marks_full_and_hands_the_piece_on(self):
        self.m.script = {"work": "limit"}
        rid, run = self.m.run([piece("a", ["a.txt"]), piece("b", ["b.txt"]), piece("c", ["c.txt"])])
        self.assertEqual(run["state"], "ready-untested", self.m.file(rid, "watcher.log"))
        moved = [p for p in run["pieces"] if len(p["attempts"]) == 2]
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0]["attempts"][0]["account"], "work")
        self.assertNotEqual(moved[0]["attempts"][1]["account"], "work")
        until = self.m.full()["work"]["until"]
        self.assertEqual(time.localtime(until)[3:5], (17, 0))  # "resets 5pm", in local time
        self.assertIn("A previous worker on another account stopped",
                      self.m.file(rid, os.path.join("logs", moved[0]["id"] + ".2.prompt.md")))
        self.assertFalse(run["advance"]["done"])  # no project test: the owner's branch is left alone
        self.assertIn("work -> ", self.m.file(rid, "REPORT.md"))
        self.assertIn("full until", self.m.ma("accounts").stdout)
        self.assertIn("(limit seen today ", self.m.ma("accounts").stdout)
        # until the reset, a new run never starts a piece on the full account
        self.m.script = {}
        before = len(self.m.calls())
        rid2, run2 = self.m.run([piece(c, [c + "2.txt"]) for c in "def"])
        self.assertEqual(run2["state"], "ready-untested")
        self.assertNotIn("work", [l["account"] for l in self.m.calls()[before:]])
        # after the reset it is ready again
        marks = self.m.full()
        marks["work"]["until"] = time.time() - 1
        json.dump(marks, open(os.path.join(self.m.data, "full.json"), "w"))
        self.assertIn("3 accounts ready", self.m.ma("accounts").stdout)

    def test_a_full_account_gets_no_more_pieces_in_the_same_run(self):
        self.m.script = {"work": "limit"}
        rid, run = self.m.run([piece(c, [c + ".txt"]) for c in "abcdef"], max_local=1)  # one at a time
        self.assertEqual(run["state"], "ready-untested")
        self.assertEqual(sum(1 for l in self.m.calls() if l["account"] == "work"), 1)

    def test_no_account_ready(self):
        for n in ("main", "work", "team"):
            subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); import accounts; "
                            "accounts.mark_full(%r, 'resets 11pm')" % (LIB, n)], env=self.m.env(), check=True)
        r = self.m.ma("split", "--plan", self.m.plan([piece("a", ["a.txt"])]), "--repo", self.m.repo, "--no-watcher")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no account can take work now", r.stderr)
        r = self.m.ma("split", "--plan", self.m.plan([piece("a", ["a.txt"])]), "--repo", self.m.repo, "--no-watcher",
                      "--account", "team")
        self.assertIn("'team' cannot take work now: full until", r.stderr)

    def test_failed_test_leaves_your_branch(self):
        base = self.m.git("rev-parse", "HEAD").strip()
        rid, run = self.m.run([piece("a", ["a.txt"]), piece("b", ["b.txt"])], test="false")
        self.assertEqual(run["state"], "test-failed")
        self.assertFalse(run["advance"]["done"])
        self.assertEqual(self.m.git("rev-parse", "HEAD").strip(), base)
        self.assertIn("failed", self.m.file(rid, "REPORT.md"))

    def test_if_you_moved_your_branch_it_is_left_alone(self):
        rid = self.m.start([piece("a", ["a.txt"])], test="true")
        open(os.path.join(self.m.repo, "mine.txt"), "w").write("I did this meanwhile\n")
        self.m.git("add", "mine.txt")
        self.m.git("commit", "-q", "-m", "mine")
        mine = self.m.git("rev-parse", "HEAD").strip()
        self.m.ma("watch", rid)
        run = self.m.load(rid)
        self.assertFalse(run["advance"]["done"])
        self.assertIn("moved", run["advance"]["reason"])
        self.assertEqual(self.m.git("rev-parse", "HEAD").strip(), mine)

    def test_report_in_the_answer_when_it_could_not_write_it(self):
        self.m.script = {"piece:a": "no-report"}
        rid, run = self.m.run([piece("a", ["a.txt"])], test="test -f a.txt")
        self.assertEqual(run["state"], "ready")
        self.assertTrue(run["pieces"][0]["attempts"][0]["report_in_answer"])

    def test_reports_and_worktrees_outside_every_claude_folder(self):
        rid, run = self.m.run([piece("a", ["a.txt"])])
        rep = run["pieces"][0]["attempts"][0]["report"]
        self.assertTrue(rep.startswith(os.path.join(self.m.data, "worktrees")), rep)
        self.assertNotIn("/.claude", rep)
        self.assertNotIn("/.claude", run["pieces"][0]["worktree"])

    def test_two_failures_is_failed_and_your_branch_is_left_alone(self):
        base = self.m.git("rev-parse", "HEAD").strip()
        self.m.script = {"piece:b": "nothing"}
        rid, run = self.m.run([piece("a", ["a.txt"]), piece("b", ["b.txt"])], test="true")
        self.assertEqual(run["state"], "finished-with-failures")
        b = [p for p in run["pieces"] if p["id"] == "b"][0]
        self.assertEqual(b["state"], "failed")
        self.assertEqual(len(b["attempts"]), 2)
        self.assertNotEqual(b["attempts"][0]["account"], b["attempts"][1]["account"])
        self.assertEqual(self.m.git("rev-parse", "HEAD").strip(), base)

    def test_a_conflict_when_joining_becomes_a_join_piece(self):
        self.m.script = {"piece:a": "outside", "piece:b": "outside"}
        rid, run = self.m.run([piece("a", ["a.txt"]), piece("b", ["b.txt"])], test="test -f a.txt && test -f b.txt")
        fix = [p for p in run["pieces"] if p["kind"] == "join"]
        self.assertEqual(len(fix), 1)
        self.assertEqual(fix[0]["state"], "done")
        self.assertEqual(fix[0]["files"], ["shared.txt"])
        self.assertEqual(run["state"], "ready")
        self.assertIn("resolved", open(os.path.join(self.m.repo, "shared.txt")).read())

    def test_shared_files_are_refused(self):
        r = self.m.ma("split", "--plan", self.m.plan([piece("a", ["src/"]), piece("b", ["src/x.py"])]),
                      "--repo", self.m.repo, "--no-watcher")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("share", r.stderr)

    def test_uncommitted_work_is_saved_before_starting(self):
        open(os.path.join(self.m.repo, "half_done.txt"), "w").write("work with no commit\n")
        rid, run = self.m.run([piece("a", ["a.txt"])])
        self.assertTrue(run["wip"])
        self.assertIn("half_done.txt", self.m.git("show", "--name-only", run["wip"]))

    def test_a_worker_gets_only_the_fixed_variables(self):
        # neither the parent session's variables nor any other one off the fixed list reach a worker
        rid, run = self.m.run([piece("a", ["a.txt"])], CLAUDECODE="1", CLAUDE_CODE_SESSION_ID="parent-session",
                              CLAUDE_CODE_ENTRYPOINT="cli", ANTHROPIC_MODEL="parent-model", MA_UNLISTED="x")
        self.assertEqual(run["state"], "ready-untested", self.m.file(rid, "watcher.log"))
        self.assertEqual([l["leak"] for l in self.m.calls()], [[]])

    def test_at_most_six_workers_at_once_and_stop(self):
        rid = self.m.start([piece(c, [c + ".txt"]) for c in "abcdefgh"])
        self.m.script = {c: "sleep" for c in ("main", "work", "team")}
        self.m.ma("watch", rid, "--once")
        run = self.m.load(rid)
        self.assertEqual(run["max_local"], 6)
        self.assertEqual(sum(1 for p in run["pieces"] if p["state"] == "working"), 6)
        open(os.path.join(self.m.data, "runs", rid, "STOP"), "w").close()
        self.m.ma("watch", rid, "--once")
        run = self.m.load(rid)
        self.assertEqual(run["state"], "stopped")
        time.sleep(0.5)
        for p in run["pieces"]:
            if p.get("pid"):
                self.assertNotEqual(subprocess.run(["ps", "-p", str(p["pid"])], capture_output=True).returncode, 0,
                                    "worker %s is still alive" % p["id"])


class Artifacts(unittest.TestCase):
    """Every account's artifacts from a folder of local copies."""

    def setUp(self):
        self.m = World()
        self.folder = os.path.join(self.m.home, "copies")
        self.keep("AAA", "~/.claude", "20261020-101500", "<title>Store plan</title>", caps=None)
        self.keep("BBB", "~/.claude-work", "20261021-120300", "<title>Mockup &amp; screens</title>",
                  caps={"db": {}}, more=[("~/.claude-work", "20261021-130000", ["photo.png"])])
        self.keep("CCC", "~/.claude-work", "20261019-080000", None, caps=None)
        # AAA was first published as another page: the newest publish's page is the one shown
        d = os.path.join(self.folder, "AAA")
        open(os.path.join(d, "old.html"), "w").write("<title>Old version</title>")
        meta = json.load(open(os.path.join(d, "_meta.json")))
        meta["publishes"].insert(0, {"when": "20261018-090000", "files": ["old.html"], "account": "~/.claude"})
        json.dump(meta, open(os.path.join(d, "_meta.json"), "w"))

    def tearDown(self):
        self.m.remove()

    def keep(self, aid, account, when, title, caps, more=()):
        d = os.path.join(self.folder, aid)
        os.makedirs(d)
        pubs = [{"when": when, "files": ["index.html"] if title else ["data.json"], "description": "desc " + aid,
                 "capabilities": caps, "account": account}]
        pubs += [{"when": w, "files": f, "description": None, "capabilities": None, "account": c} for c, w, f in more]
        if title:
            open(os.path.join(d, "index.html"), "w").write("<html><head>%s</head></html>" % title)
        json.dump({"url": ARTIFACT_URL + aid, "publishes": pubs}, open(os.path.join(d, "_meta.json"), "w"))

    def ma(self, *a):
        return self.m.ma(*a, MULTI_ACCOUNT_ARTIFACTS=self.folder)

    def test_every_account_newest_first(self):
        lst = json.loads(self.ma("artifacts", "--json").stdout)
        self.assertEqual([a["id"] for a in lst], ["BBB", "AAA", "CCC"])
        by = {a["id"]: a for a in lst}
        self.assertEqual(by["AAA"]["account"], "main")
        self.assertEqual(by["AAA"]["title"], "Store plan")
        self.assertEqual(by["BBB"]["account"], "work")
        self.assertEqual(by["BBB"]["title"], "Mockup & screens")
        self.assertTrue(by["BBB"]["copy"].endswith("BBB/index.html"))  # an image publish later does not hide the page
        self.assertTrue(by["BBB"]["live"])
        self.assertIsNone(by["CCC"]["copy"])
        self.assertEqual(by["CCC"]["title"], "desc CCC")

    def test_search_and_filter_by_account(self):
        self.assertEqual([a["id"] for a in json.loads(self.ma("artifacts", "--json", "--search", "store").stdout)],
                         ["AAA"])
        self.assertEqual([a["id"] for a in json.loads(self.ma("artifacts", "--json", "--account", "work").stdout)],
                         ["BBB", "CCC"])

    def test_view_opens_the_copy_or_gives_the_link(self):
        r = self.ma("view", "AAA")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(os.path.join(self.folder, "AAA", "index.html"), r.stdout)
        r = self.ma("view", "CCC")
        self.assertIn(ARTIFACT_URL + "CCC", r.stdout)
        self.assertNotEqual(self.ma("view", "ZZZ").returncode, 0)

    def test_gallery_has_every_artifact_and_the_pane_the_summary(self):
        r = self.ma("gallery", "--no-open")
        self.assertEqual(r.returncode, 0, r.stderr)
        html = open(os.path.join(self.m.data, "artifacts.html")).read()
        data = json.loads(html.split('type="application/json">', 1)[1].split("</script>", 1)[0])
        self.assertEqual([a["t"] for a in data["a"]], ["Mockup & screens", "Store plan", "desc CCC"])
        self.assertEqual(data["p"], {"work": 2, "main": 1})
        self.assertTrue(data["a"][0]["v"].startswith("file://"))
        pane = json.loads(self.ma("pane").stdout)["artifacts"]
        self.assertEqual(pane["total"], 3)
        self.assertEqual(pane["newest"][0]["id"], "BBB")

    def test_no_folder_lists_nothing(self):
        r = self.m.ma("artifacts")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("No artifact copies found", r.stdout)


if __name__ == "__main__":
    unittest.main()
