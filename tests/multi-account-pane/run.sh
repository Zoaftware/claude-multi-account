#!/bin/sh
# Runs the pane's tests against a temporary copy of the plugin, so the plugin folder itself carries no test files.
set -e
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cp -R "$root/plugins/multi-account-pane" "$work/multi-account-pane"
cp "$here"/*.test.tsx "$work/multi-account-pane/hooks/"
claude plugin test "$work/multi-account-pane"
