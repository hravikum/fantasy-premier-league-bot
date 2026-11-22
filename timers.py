"""Starts the bot's GitHub runs from cron-job.org: one "tick" every 30 minutes.

GitHub's own schedule started the ESPN bot's runs 7 1/2 to 9 hours late, so cron-job.org asks
GitHub to run bot.yml's tick job instead (the same as Run workflow on the Actions page). Each tick
looks at FPL's next deadline and runs the job that's due, if any (schedule.py); the rest finish in
under a minute. Deadlines move around, so fixed times per job wouldn't work.

Run it on the Mac once (and again if TIMERS changes). It creates or updates its job and removes any
it made that TIMERS no longer lists:

    .venv/bin/python3 timers.py --dry-run     # what it would send, with the GitHub token hidden
    .venv/bin/python3 timers.py

It reads two keys, from .env or the environment:
    CRONJOB_API_KEY         cron-job.org > Console > Settings > API (the same key as the ESPN bot)
    GITHUB_DISPATCH_TOKEN   a fine-grained GitHub token for this repository, with Actions: read and
                            write. cron-job.org keeps it and sends it with each request.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

REPO = "hravikum/fantasy-premier-league-bot"
WORKFLOW = f"https://api.github.com/repos/{REPO}/actions/workflows/bot.yml"
CRONJOB = "https://api.cron-job.org"
TITLE = "FPL bot: "              # every job this script owns starts with this, and only those are touched
EASTERN = "America/New_York"
EVERY = [-1]

# (title, bot.yml job, weekdays 0 = Sunday, hours, minutes), Eastern time. :13 and :43 rather than
# :00 and :30, when everyone else's jobs fire.
TIMERS = [
    ("tick every 30 minutes", "tick", EVERY, EVERY, [13, 43]),
]


def job(title, name, wdays, hours, minutes, token):
    """One cron-job.org job: at these times, ask GitHub to run bot.yml's `name` job on main."""
    return {
        "title": TITLE + title,
        "url": f"{WORKFLOW}/dispatches",
        "enabled": True,
        "saveResponses": True,
        "requestMethod": 1,  # POST
        "schedule": {"timezone": EASTERN, "expiresAt": 0, "months": EVERY, "mdays": EVERY,
                     "wdays": wdays, "hours": hours, "minutes": minutes},
        "extendedData": {
            "headers": {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"},
            "body": json.dumps({"ref": "main", "inputs": {"job": name}}),
        },
        # An email at the first failed request, when one succeeds again, and if cron-job.org gives up.
        "notification": {"onFailure": True, "onFailureCount": 1, "onSuccess": True, "onDisable": True},
    }


def cronjob(method, path, key, body=None):
    response = requests.request(method, CRONJOB + path, json=body, timeout=30,
                                headers={"Authorization": f"Bearer {key}"})
    if response.status_code != 200:
        sys.exit(f"cron-job.org {method} {path} answered {response.status_code}: {response.text[:300]}")
    return response.json()


def check_token(token):
    """Fail before touching cron-job.org if the token can't see the workflow it will start."""
    response = requests.get(WORKFLOW, timeout=30, headers={"Authorization": f"Bearer {token}",
                                                           "Accept": "application/vnd.github+json"})
    if response.status_code != 200:
        sys.exit(f"GitHub answered {response.status_code} for bot.yml with GITHUB_DISPATCH_TOKEN: is it for "
                 f"{REPO}, with Actions: read and write, and not expired?")


def main(dry_run=False):
    load_dotenv()
    token, key = os.getenv("GITHUB_DISPATCH_TOKEN"), os.getenv("CRONJOB_API_KEY")
    wanted = [job(*timer, token=token or "") for timer in TIMERS]
    if dry_run:
        for each in wanted:
            each["extendedData"]["headers"]["Authorization"] = "Bearer <GITHUB_DISPATCH_TOKEN>"
        print(json.dumps(wanted, indent=2))
        return
    missing = [name for name, value in (("GITHUB_DISPATCH_TOKEN", token), ("CRONJOB_API_KEY", key)) if not value]
    if missing:
        sys.exit(f"Set {' and '.join(missing)} in .env first.")
    check_token(token)

    ours = {j["title"]: j["jobId"] for j in cronjob("GET", "/jobs", key)["jobs"] if j["title"].startswith(TITLE)}
    created = 0
    for each in wanted:
        if each["title"] in ours:
            cronjob("PATCH", f"/jobs/{ours.pop(each['title'])}", key, {"job": each})
            print(f"updated  {each['title']}")
        else:
            if created:
                time.sleep(13)  # cron-job.org takes at most 5 new jobs a minute
            cronjob("PUT", "/jobs", key, {"job": each})
            created += 1
            print(f"created  {each['title']}")
    for title, job_id in ours.items():
        cronjob("DELETE", f"/jobs/{job_id}", key)
        print(f"removed  {title} (no longer in TIMERS)")

    print("\nNext runs (Eastern):")
    for each in sorted((j for j in cronjob("GET", "/jobs", key)["jobs"] if j["title"].startswith(TITLE)),
                       key=lambda j: j.get("nextExecution") or 0):
        when = each.get("nextExecution")
        when = datetime.fromtimestamp(when, ZoneInfo(EASTERN)).strftime("%a %b %d %I:%M %p") if when else "-"
        print(f"  {when}  {each['title']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Set up the cron-job.org timer that starts the bot's GitHub runs.")
    parser.add_argument("--dry-run", action="store_true", help="print the job it would send, and change nothing")
    main(parser.parse_args().dry_run)
