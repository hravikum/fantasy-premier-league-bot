#!/bin/bash
# Runs the bot on GitHub's machines (.github/workflows/bot.yml says when).
#
# GitHub starts every run on a fresh machine, so state/ (the encrypted login token) lives on the
# bot-state branch: this brings it back, runs the bot, and saves state/ again even when the run
# failed. The login token changes on every run, and losing the new one means signing in again.
#
# Usage: hosted_run.sh [bot.py options]
set -uo pipefail
cd "$(dirname "$0")" || exit 1

STATE="${RUNNER_TEMP:-/tmp}/bot-state"
rm -rf "$STATE"
if git fetch -q --depth=1 origin bot-state 2>/dev/null; then
    git worktree add -q --detach "$STATE" FETCH_HEAD
else
    echo "No bot-state branch yet: this is the first run, so the login comes from FPL_REFRESH_TOKEN."
    git worktree add -q --orphan -b bot-state "$STATE"
fi
mkdir -p state
if [ -d "$STATE/state" ]; then cp -R "$STATE/state/." state/; fi

python bot.py "$@"
STATUS=$?

mkdir -p "$STATE/state"
cp -R state/. "$STATE/state/"
git -C "$STATE" add -A
if ! git -C "$STATE" diff --cached --quiet; then
    git -C "$STATE" -c user.name="fpl bot" -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
        commit -qm "run $(date '+%a %b %d %H:%M')"
    if ! git -C "$STATE" push -q origin HEAD:refs/heads/bot-state; then
        echo "Could not save the login token to the bot-state branch: the next run may need a fresh sign-in." >&2
        STATUS=1
    fi
fi
exit "$STATUS"
