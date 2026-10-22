---
name: split
description: Run one task on every connected Claude account at the same time. Splits the task into independent pieces (each with its own files), gives each piece its own git worktree and an account, moves a piece to another account when one hits its usage limit, joins everything in a separate worktree, runs the project's test and reports. Accounts can have roles for the task (one only plans, one only designs, one only develops, one only audits). Use when the user types /multi-account:split <task>, or asks to "run this on all my accounts", "split this task across my accounts", "have every account work on this".
argument-hint: "[--account <name>] [--roles name=role ...] [--stages plan,design,audit] <the task, in your words>"
---

# Run one task on every account at once

`MA` below means `python3 "${CLAUDE_PLUGIN_ROOT}/lib/ma.py"`. If `${CLAUDE_PLUGIN_ROOT}` was not replaced by a real
path, use `python3 "$(ls -d ~/.claude/plugins/cache/*/multi-account/*/ | tail -1)lib/ma.py"`.

The task, in the user's words: `$ARGUMENTS`

If it starts with `--account <name>`, the user picked that ONE account (they typed "<name>: ..." in the chat, or said
"have <name> do it"): the task is the rest of the text, and every piece runs on that account (`--account <name>` in
step 4). Otherwise every ready account shares it.

**Roles for this task.** The user may set, when the task starts, which account does which stage: `--roles work=audit
team=dev`, or in words ("have work only audit", "main plans and team develops", "one account only designs"). Roles are
`plan`, `design`, `dev`, `audit` (`general` = any stage). Pass them as `--roles` in step 4: they hold for this task
only. If the user says they are for every task ("always", "from now on"), save them first with
`MA roles <name>=<role> ...` and pass nothing. Stages with no role behind them can still be asked for ("with an
audit", "have someone plan it first"): `--stages audit` (or `plan`, `design`); any account with no role does them.
How a task flows: planning -> design -> development -> audit, each stage on the accounts allowed to take it. The
planner splits the task into pieces, the designer writes the design every developer reads, the developers build the
pieces at the same time, and the auditor reads the joined work: what it finds goes to a developer and it audits again
(3 audits at most). Planner, designer and auditor only read the project; the user's branch moves only when the audit
passed. With `--account <name>`, roles do not apply.

## Steps

1. **Accounts.** Run `MA accounts` (its **Role** column shows saved roles). With no account ready, say when the first
   full one frees up and how to connect another (`/multi-account:accounts connect`), then stop. With
   `--account <name>`, that account must be ready; if it is not, say why and which account is ready now, then stop.
2. **The project.** Its root is `git rev-parse --show-toplevel` of the current folder, unless the task names another.
   Read enough of the project to split the task: its layout, the files the task touches, how it is tested.
3. **The plan.** Write `plan.json` in your scratchpad folder:

   ```json
   {"task": "<the user's words, verbatim>",
    "test": "<one command that checks the whole project, or leave this key out>",
    "pieces": [{"id": "a", "title": "<short title the user will read>",
                "todo": ["<concrete step 1, with how to check it>", "<step 2>"],
                "files": ["src/feature_a.py", "tests/test_feature_a.py"],
                "check": "python3 -m pytest tests/test_feature_a.py"}]}
   ```

   - Pieces are independent: no two pieces list the same file or folder (the script refuses the plan otherwise).
   - At least as many pieces as ready accounts when the task allows it; more is fine, they wait their turn.
   - Each piece is at most ~40 minutes of work; its TODO items are concrete and say how to check them.
   - `check` is one simple command (no `;`, `&`, `|`, `$`) that a worker may run in its folder; leave it out if there
     is none. Workers may only edit their own files and use git in their own worktree, so a piece never needs a
     server, a browser or a package install.
   - If the task cannot be split into independent pieces, make one piece.
   - **When an account plans** (a `plan` role, saved or in `--roles`, or `--stages plan`): do not split the task
     yourself; write `"pieces": []` and let that account read the project and make the pieces. Keep `task` and `test`.
4. **Start it** (no confirmation question: the user already asked for it):
   `MA split --plan <plan.json> --repo <root> [--test "<cmd>"] [--account <name>] [--roles name=role ...]
   [--stages ...]`
   It saves uncommitted work in a wip commit first, prints which account took which piece (or which account starts
   planning or designing), the stages and the roles, and `ID=<run id>`. If it refuses because a stage has nobody to do
   it ("nobody can do the ... stage"), say so plainly with the fix it gives, and stop.
5. **Wait without polling.** Start `MA wait <run id>` with the Bash tool's `run_in_background`, so this session is told
   when the run ends. Tell the user, in a few lines: how many pieces on which accounts, `/multi-account:status` to
   follow it, and that they can keep working.
6. **When it ends**, show the report (`REPORT.md`): one line with the result, what changed (their branch moved
   forward, or the joined branch waits for review, and why), what each audit round found, and what is left. A piece
   or a test that failed is said plainly, with its reason; never call it done.
