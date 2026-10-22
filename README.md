# Claude multi-account

Two Claude Code plugins for people who have more than one Claude account of their own:

- **multi-account**: connect your accounts one at a time, then run one task on all of them at once. The task is split
  into independent pieces, each piece gets its own git worktree and an account, a piece moves to another account when
  one hits its usage limit, and everything is joined and tested at the end in a separate worktree.
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
network call of its own.

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
another account, runs the project's test, and moves your branch forward only when the test passed and you did not
move your branch in the meantime. `/multi-account:status` shows the run and stops it.

**When an account is full.** When a worker prints Claude Code's usage-limit message, that account is marked full until
the reset time the message names (for example "resets 5pm", read in your local time), or for one hour when it names
none. Its piece moves to another ready account, and no new piece starts on it until that time has passed.

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
    claude plugin test plugins/multi-account-pane

## License

MIT. Made by [Zoaftware](https://zoaftware.com).
