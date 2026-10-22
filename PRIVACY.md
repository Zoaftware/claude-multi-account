# Privacy

The multi-account plugins collect no data about you and send nothing to Zoaftware or to anyone else.

- **No network calls of their own.** The plugins run on your computer only. The `claude` processes they start talk to
  Anthropic exactly as Claude Code always does, each under the account signed in to its own config folder and that
  account's own terms.
- **No passwords or tokens.** The plugins never read, store or send a password or a token, and never open Claude
  Code's own files. To know who is signed in to a folder they ask Claude Code (`claude auth status --json`) and keep
  only the email.
- **What is stored, and where.** The list of your accounts (name, folder, email), run plans, worker logs and reports,
  in `~/.multi-account` (or `$MULTI_ACCOUNT_HOME`) on your own computer. Delete that folder to remove all of it.
- **No analytics, no telemetry, no ads.**

Questions: open an issue at https://github.com/Zoaftware/claude-multi-account/issues.

Made by [Zoaftware](https://zoaftware.com).
