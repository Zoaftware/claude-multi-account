---
name: artifacts
description: List the artifacts of every connected account in one place, without switching accounts - a local page with search, a filter per account, each one's local copy and its claude.ai link - or open one. Use when the user types /multi-account:artifacts, or asks for "my artifacts", "artifacts from my other account", "find the artifact about X", "open artifact X".
argument-hint: "[search <text> | account <name> | view <id>]"
---

# Every account's artifacts

`MA` below means `python3 "${CLAUDE_PLUGIN_ROOT}/lib/ma.py"`. If `${CLAUDE_PLUGIN_ROOT}` was not replaced by a real
path, use `python3 "$(ls -d ~/.claude/plugins/cache/*/multi-account/*/ | tail -1)lib/ma.py"`.

Arguments: `$ARGUMENTS`

| Arguments | Run |
| --- | --- |
| (none) | `MA gallery` (writes the page and opens it in the browser), then `MA artifacts --n 8` |
| `search <text>` | `MA artifacts --search "<text>"` |
| `account <name>` | `MA artifacts --account <name>` |
| `view <id>` | `MA view <id>` (opens the local copy; with no copy it prints the claude.ai link) |

Where the copies come from: a folder of local copies the user keeps (`$MULTI_ACCOUNT_ARTIFACTS`, else
`~/.multi-account/artifacts`), one subfolder per artifact with its published files and a `_meta.json`
(`{"url": ..., "publishes": [{"when": "YYYYMMDD-HHMMSS", "files": [...], "account": "~/.claude-work", ...}]}`).
The plugin only reads that folder. Say these limits in one line when they matter:
- an artifact with no saved copy shows only its claude.ai link;
- a local copy opens with no account, so a page with live data (a form, a shared list) opens without that data;
- the claude.ai link opens the original, which asks for the account that owns it.

Never call claude.ai with a stored login to list another account's artifacts: each account's own session reads its
own (`Artifact list`).

Reply with one line (how many artifacts, per account, and that the page is open), then where to click.
