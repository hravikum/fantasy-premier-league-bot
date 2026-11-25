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
RUN_URL="${GITHUB_SERVER_URL:-}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}"
RETRY_WAITS="${RETRY_WAITS:-0 10 30}"     # seconds before each try

alert() {   # alert SUBJECT BODY: email without ever failing the script
    python -c "import sys, mailer; mailer.send(sys.argv[1], sys.argv[2])" "$1" "$2 Run: $RUN_URL" || true
}

# Bring back state/. A failed fetch is not the same as no branch: GitHub sometimes doesn't answer,
# and starting from scratch then would mean using the old FPL_REFRESH_TOKEN, which is used up.
# ls-remote exits with 2 only when GitHub answered and there is no bot-state branch.
STATE="${RUNNER_TEMP:-/tmp}/bot-state"
rm -rf "$STATE"
git worktree prune
FOUND=""
for wait in $RETRY_WAITS; do
    sleep "$wait"
    if git fetch -q --depth=1 origin bot-state; then FOUND=yes; break; fi
    git ls-remote --exit-code --heads origin bot-state >/dev/null
    if [ $? -eq 2 ]; then FOUND=none; break; fi
    echo "Couldn't fetch bot-state (see above); trying again." >&2
done
case "$FOUND" in
    yes)  git worktree add -q --detach "$STATE" FETCH_HEAD ;;
    none) echo "No bot-state branch yet: this is the first run, so the login comes from FPL_REFRESH_TOKEN."
          git worktree add -q --orphan -b bot-state "$STATE" ;;
    *)    PROBLEM="GitHub didn't hand over the bot's saved state (bot-state branch), so this run stopped before doing anything. Nothing was changed on FPL and the saved login is untouched. The next tick, in 30 minutes, tries again: this only needs you if it keeps happening within 3 hours of a deadline."
          echo "$PROBLEM" >&2
          alert "the bot skipped a run (GitHub didn't answer)" "$PROBLEM"
          exit 1 ;;
esac
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
    TOKEN_CHANGED=""
    git -C "$STATE" diff --cached --quiet -- state/token.enc || TOKEN_CHANGED=yes
    git -C "$STATE" -c user.name="fpl bot" -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
        commit -qm "run $(date '+%a %b %d %H:%M')"
    SAVED=""
    for wait in $RETRY_WAITS; do
        sleep "$wait"
        if git -C "$STATE" push -q origin HEAD:refs/heads/bot-state; then SAVED=yes; break; fi
    done
    if [ -z "$SAVED" ]; then
        STATUS=1
        if [ -n "$TOKEN_CHANGED" ]; then
            PROBLEM="This run renewed the FPL login but couldn't save the new token to the bot-state branch, so the next run will probably fail to log in. Sign in again (README: Log in once) before the next deadline."
            echo "$PROBLEM" >&2
            alert "ACTION NEEDED: the bot could not save its login" "$PROBLEM"
        else
            echo "Couldn't save state/ to bot-state, but the login token didn't change, so nothing is lost." >&2
        fi
    fi
fi
exit "$STATUS"
