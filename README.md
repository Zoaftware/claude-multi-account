# Claude multi-account

Two Claude Code plugins for people who have more than one Claude account of their own:

- **multi-account**: connect your accounts one at a time, then run one task on all of them at once. The task is split
  into independent pieces, each piece gets its own git worktree and an account, a piece moves to another account when
  one hits its usage limit, and everything is joined and tested at the end in a separate worktree. Accounts can have
  roles: one only plans, one only designs, one only develops, one only audits.
- **multi-account-pane**: a pane beside the chat with every account's state, the last run and the newest artifacts.
  Start a chat message with `all:` to split it across your accounts, or with an account's name (`work:`) to send it
  to that one account.

> **Each account must be your own, on its own paid plan. Never share an account with another person.**
> This plugin does not raise, pool or work around any usage limit: each account keeps its own limits, and an account
> that reaches its limit simply stops taking new pieces until it resets.

## Install

In Claude Code:

    /plugin marketplace add Zoaftware/claude-multi-account
    /plugin install multi-account@zoaftware
    /plugin install multi-account-pane@zoaftware

Or in one step each:

    /plugin install multi-account --marketplace Zoaftware/claude-multi-account
    /plugin install multi-account-pane --marketplace Zoaftware/claude-multi-account

From a shell:

    claude plugin install multi-account --marketplace Zoaftware/claude-multi-account
    claude plugin install multi-account-pane --marketplace Zoaftware/claude-multi-account

In the desktop app, where `/plugin` does not run: click **+** next to the prompt box, then **Plugins**, **Add
marketplace** with `Zoaftware/claude-multi-account`, then **Add plugin**.

It needs `git` and `python3` (3.8 or newer, standard library only). Opening a sign-in window by itself works on
macOS; elsewhere the plugin prints the command to run.

## How it works

**Accounts.** Your main account is `~/.claude`. Each other account is a config folder of its own,
`~/.claude-<name>`, used through `CLAUDE_CONFIG_DIR`. `/multi-account:accounts connect` creates a new, empty folder
(nothing is copied or linked from another account) and opens a window where you sign in:

    CLAUDE_CONFIG_DIR="$HOME/.claude-<name>" claude

The plugin never reads, stores or sends a password or a token, and it never opens Claude Code's own files. To know
who is signed in to a folder it asks Claude Code itself (`claude auth status --json` for that folder) and keeps only
the email, so two folders with the same login count once ("duplicate of main"). It never measures usage and makes no
network call of its own. The table marks the account the current session runs on ("this app" in the Claude app), from
the email Claude Code gives its own tools. The Claude app writes its own account into `~/.claude`, so when another
folder holds that account, that folder owns it and main shows "login unknown in the Claude app" and takes no work.

Each `claude` it starts for an account gets only a short, fixed list of environment variables (path, home, locale,
temp folder) plus that account's `CLAUDE_CONFIG_DIR`, never your whole environment, so nothing from the session you
start it from (`CLAUDE_CODE_*`, `ANTHROPIC_*`) reaches it. Behind a proxy or with your own certificates, put those
settings (`HTTPS_PROXY`, `NODE_EXTRA_CA_CERTS`, ...) in the `env` of each account's `settings.json`
(`~/.claude-<name>/settings.json`), where Claude Code reads them.

**Running a task.** `/multi-account:split <task>` (or a chat message that starts with `all:`) plans independent pieces,
saves your uncommitted work in a wip commit, and starts one headless `claude -p` worker per piece, at most 6 at once
on this computer, spread evenly over the ready accounts. Each worker may only edit its piece's files and use git in
its own worktree (permission mode `acceptEdits` with a fixed list of allowed tools). A detached watcher checks them,
joins the finished branches in plan order into `ma/<run>/joined`, turns a merge conflict into a join piece for
another account, runs the project's test, and moves your branch forward only when the test passed (and the audit,
when there is one) and you did not move your branch in the meantime. `/multi-account:status` shows the run and stops
it.

**Roles: one account plans, one designs, one develops, one audits.** An account can be limited to one stage of every
task, saved with `/multi-account:accounts roles work=audit team=dev` (`general` or `--clear` takes it away), or set
for one task only with `split --roles ...`. A task then goes through its stages in order:

- **plan**: the planner reads the project and writes the pieces (each with its own files) for the developers.
- **design**: the designer writes the design that every developer's prompt points to.
- **dev**: the developers build the pieces at the same time, one worktree each.
- **audit**: the auditor reads the joined work, its diff and the project's test result, and answers PASS or FAIL with
  its findings. A FAIL goes to a developer as a fix on the joined branch, then the auditor checks again (3 audits at
  most, `MA_AUDIT_ROUNDS`). Your branch moves only when the audit passed.

An account with a role takes only that stage; an account with no role takes any stage, but only when no account with
that stage's role is ready. A run with a stage nobody may ever take is refused before it starts. Planner, designer and
auditor only read the project (Read, Glob, Grep and read-only git, working from the run's report folder); one that
changes a project file fails at once and its stage is not handed on. `split --stages audit` (or `plan`, `design`) adds
a stage with no role behind it, done by an account with no role. With `--account`, roles do not apply.

**When an account is full.** When a worker prints Claude Code's usage-limit message, that account is marked full until
the reset time the message names (for example "resets 5pm", read in your local time), or for one hour when it names
none. Its piece moves to another ready account, and no new piece starts on it until that time has passed. The
accounts table says both times, for example `full until today 17:00 (limit seen today 14:12)`.

**Data folder.** Everything the plugin writes lives in `~/.multi-account` (or `$MULTI_ACCOUNT_HOME`), outside every
`.claude` folder: `accounts.json`, `full.json`, `runs/<id>/` (the run, its watcher log, worker logs and
`REPORT.md`) and `worktrees/<id>/` (or `$MULTI_ACCOUNT_WORKTREES`).

**Artifacts.** `/multi-account:artifacts` lists every account's artifacts in one local page, from a folder of copies
that you fill (`$MULTI_ACCOUNT_ARTIFACTS`, else `~/.multi-account/artifacts`). The plugin only reads it:

    artifacts/
      <artifact id>/
        _meta.json      {"url": "https://claude.ai/artifact/<id>",
                         "publishes": [{"when": "20261021-120300", "files": ["index.html"],
                                        "account": "~/.claude-work", "description": "..."}]}
        index.html      the published files of each publish

## Tests

No AI, no sign-in, no network: the engine's tests use a fake `claude` and a fake home folder. They live in
`tests/`, outside the plugin folder, so installing the plugin copies only the code that runs.

    python3 tests/multi-account/test_multi_account.py -v
    sh tests/multi-account-pane/run.sh

## Privacy, terms and support

[Privacy](PRIVACY.md) · [Terms of use](TERMS.md) · Questions and bugs: [issues](https://github.com/Zoaftware/claude-multi-account/issues).

## License

MIT. Made by [Zoaftware](https://zoaftware.com).
