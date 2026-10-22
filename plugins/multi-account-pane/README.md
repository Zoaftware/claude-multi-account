# multi-account-pane

A pane beside the Claude Code chat for the `multi-account` plugin, and the chat itself as its task field. It needs
`multi-account` 1.0.0 or newer installed.

    /plugin install multi-account-pane --marketplace Zoaftware/claude-multi-account

## The chat sends the work

- A message that starts with **`all:`** is split across every ready account, at the same time
  (`/multi-account:split`).
- A message that starts with an account's name, like **`work:`**, goes whole to that one account
  (`/multi-account:split --account work`).
- Any other message stays in this session, as always.

While typing, the route word is painted and a band above the prompt says where Enter sends the task (the accounts
that will take it, or why that account will not take it now). The first word's autocomplete offers `all:` and every
connected account. A route word with no task after it is not sent.

## The pane

Refreshed every 30 seconds:

- **Accounts**: every connected account with its state (ready, full until a time, paused, not signed in) and how many
  workers it runs. Buttons: pause or resume an account, open the sign-in window of one that is not signed in, connect
  another account (asks first). Accounts that repeat another login are listed on one dim line and never count.
- **Work from the chat**: which accounts are ready, the two route words, and a button that starts a message with
  `all:`.
- **Last run**: the last split run, each piece with its state, account and minutes. A running split can be stopped
  (asks first); finished work is kept.
- **Artifacts**: how many artifact copies each account has, the newest five with a button to open the local copy, and
  a button that builds the page with every account's artifacts.

When no surface draws the pane (it was closed, or the window is narrow), a band above the prompt has a button that
opens it again; `/accounts-pane` opens it too.

The pane runs the newest installed `multi-account` CLI (`lib/ma.py`), found by version number in any config folder.

## Tests

They live in `tests/multi-account-pane/` at the repository root, outside the plugin folder, and run against a
temporary copy of the plugin:

    sh tests/multi-account-pane/run.sh

Made by [Zoaftware](https://zoaftware.com).
