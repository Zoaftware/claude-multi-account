---
name: status
description: Show how a multi-account run is going (each piece, its account, minutes, commits, notes) or stop it. Use when the user types /multi-account:status, or asks "how is the run going", "what are my accounts working on", "stop the run".
argument-hint: "[run id] | stop [run id]"
---

# Run status

`MA` below means `python3 "${CLAUDE_PLUGIN_ROOT}/lib/ma.py"`. If `${CLAUDE_PLUGIN_ROOT}` was not replaced by a real
path, use `python3 "$(ls -d ~/.claude/plugins/cache/*/multi-account/*/ | tail -1)lib/ma.py"`.

Arguments: `$ARGUMENTS`

- (none) or an id: `MA status [<id>]` (the last run by default).
- `stop [<id>]`: `MA stop <id>` (the last run's id comes from `MA status`). It stops only that run's own workers on the
  watcher's next pass; finished pieces and their branches stay.

Reply with the table and one line on what it means: what is done, what is still working, what waits on the user.
