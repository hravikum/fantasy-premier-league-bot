#!/bin/bash
# Runs the bot on GitHub's machines (.github/workflows/bot.yml; cron-job.org starts the ticks).
#
# GitHub starts every run on a fresh machine, so state/ (the encrypted login token) lives on the
# bot-state branch: this brings it back, runs the bot, and saves state/ again even when the run
# failed. The login token changes on every run, and losing the new one means signing in again.
#
# Usage: hosted_run.sh [bot.py options], e.g. --job tick
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

# The Claude news check writes to its own branch, so it never collides with the save below.
rm -f news.json heartbeat.json
if git fetch -q --depth=1 origin bot-news 2>/dev/null; then
    git show FETCH_HEAD:news.json > news.json 2>/dev/null || rm -f news.json
    git show FETCH_HEAD:heartbeat.json > heartbeat.json 2>/dev/null || rm -f heartbeat.json
fi

python bot.py "$@"
STATUS=$?

mkdir -p "$STATE/state"
cp -R state/. "$STATE/state/"
git -C "$STATE" add -A
if ! git -C "$STATE" diff --cached --quiet; then
    git -C "$STATE" -c user.name="fpl bot" -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
        commit -qm "run $(date '+%a %b %d %H:%M')"
    if ! git -C "$STATE" push -q origin HEAD:refs/heads/bot-state; then
        PROBLEM="Could not save the bot's state (login token, schedule) to the bot-state branch. The next run may need a fresh sign-in (README: Log in once)."
        echo "$PROBLEM" >&2
        python -c "import sys, mailer; mailer.send('FPL ACTION NEEDED: the bot could not save its login', sys.argv[1])" \
            "$PROBLEM Run: ${GITHUB_SERVER_URL:-}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}" || true
        STATUS=1
    fi
fi
exit "$STATUS"
