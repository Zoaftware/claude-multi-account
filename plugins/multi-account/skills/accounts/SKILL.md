---
name: accounts
description: Show every connected Claude account (email, state, live workers), connect a new one (one at a time, as many as you own), pause, resume or remove one, or give an account a role so it only plans, designs, develops or audits. Use when the user types /multi-account:accounts, or asks to "connect another account", "add an account", "how many accounts do I have", "show my accounts", "pause account X", "make work only audit", "one account only designs", "account roles".
argument-hint: "[connect [name] | pause <name> | resume <name> | remove <name> | roles [name=role ...] [--clear]]"
---

# Accounts

`MA` below means `python3 "${CLAUDE_PLUGIN_ROOT}/lib/ma.py"`. If `${CLAUDE_PLUGIN_ROOT}` was not replaced by a real
path, use the newest installed copy: `python3 "$(ls -d ~/.claude/plugins/cache/*/multi-account/*/ | tail -1)lib/ma.py"`.

## What to run, from the arguments: `$ARGUMENTS`

| Arguments | Run |
| --- | --- |
| (none) | `MA accounts` |
| `connect` or `connect <name>` | `MA connect [<name>]` |
| `pause <name>` / `resume <name>` / `remove <name>` | `MA pause|resume|remove <name>` |
| `roles` | `MA roles` (what each account does) |
| `roles work=audit team=dev` | `MA roles work=audit team=dev` |
| `roles work=general` / `roles --clear` | take one role away / take every role away |

## Roles

An account can be limited to one stage of every task: `plan`, `design`, `dev` or `audit` (`general` takes the role
away). The user may say it in their words ("make work only audit", "team only develops"): turn that into
`MA roles <name>=<role>`. What a role means for a task:
- a task goes through its stages in order: planning -> design -> development -> audit; a role turns its stage on;
- an account with a role only takes pieces of that stage; an account with no role takes any stage, but only when no
  account with that stage's role is ready;
- planner, designer and auditor only read the project; what the auditor finds goes to a developer and it audits again
  (3 audits at most); the user's branch moves only when the audit passed;
- `MA roles` warns when a stage has nobody who can do it (every account has another role).
A role saved here applies to every task until it is taken away; for one task only, `/multi-account:split` takes
`--roles`. When some account has a role, `MA accounts` shows a **Role** column.

## Connecting an account

- `connect` creates a new, empty config folder (`~/.claude-account<N>`, or `~/.claude-<name>`) and, on macOS, opens a
  Terminal window already inside it (`CLAUDE_CONFIG_DIR="$HOME/.claude-<name>" claude`). Nothing is copied or linked
  from another account: that folder starts like a fresh install.
- The user signs in there with that account's email. Never ask for, type or read a password or a token: the sign-in
  is theirs, in their own window.
- After they sign in, run `MA connect <name>` again: it asks Claude Code who is signed in there
  (`claude auth status`, email only), refuses a login that is already connected (the same account in two folders
  counts once) and shows the account as ready.
- Each account must be the user's own, on its own paid plan. Never help share an account with another person.

## States

- **ready**: takes work.
- **full until <time> (limit seen <time>)**: one of its workers printed Claude Code's usage-limit message at the
  second time; it takes no new piece until the reset time that message named (or one hour when it named none, which
  the row says).
- **paused**, **not signed in**, **duplicate of <name>**: takes no work; say what the user can do about it.
- **login unknown in the Claude app** (main only): inside the Claude app, `~/.claude` shows the app's own account,
  and another folder already holds that account, so that folder owns it and main takes no work from here.
- **(this app)** / **(this session)** after a name: the account this session runs on.

## Reply

The table, then one line on what it means, and the one thing the user must do, if any (for example: "In the Terminal
window that just opened, sign in with that account's email.").
