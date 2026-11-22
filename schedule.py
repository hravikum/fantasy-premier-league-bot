"""When each of the bot's gameweek jobs runs, counted back from FPL's deadline.

cron-job.org starts a "tick" every 30 minutes (timers.py). Each tick asks which job is due:

  preview   from 26 h to 3 h before the deadline   the plan, and anything that needs your OK
  main      from 2 h 30 to 1 h before              fresh news, then the changes
  final     from 1 h to 10 min before              late injury check; retries the main run if it failed

Each runs once per gameweek. A job that fails isn't marked done, so the next tick in its window
tries again. state/schedule.json remembers what has run.
"""
import json
from pathlib import Path

import pandas as pd

STATE = Path(__file__).with_name("state") / "schedule.json"
H = pd.Timedelta(hours=1)
WINDOWS = {                                   # job: (opens, closes) before the deadline
    "preview": (26 * H, 3 * H),
    "main": (2.5 * H, 1 * H),
    "final": (1 * H, pd.Timedelta(minutes=10)),
}


def load(gw, deadline):
    """This gameweek's record (a new gameweek starts a fresh one)."""
    record = json.loads(STATE.read_text()) if STATE.exists() else {}
    if record.get("gw") != gw:
        record = {"gw": gw, "deadline": deadline.isoformat(), "done": {}, "failures": {}}
    record["deadline"] = deadline.isoformat()     # FPL can move a deadline
    return record


def save(record):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(record, indent=1))


def due(record, deadline, now):
    """The job to run now, or None."""
    left = deadline - now
    for job, (opens, closes) in WINDOWS.items():
        if closes <= left < opens and job not in record["done"]:
            return job
    return None


def mark(record, job, now, ok, **extra):
    if ok:
        record["done"][job] = now.isoformat()
    else:
        record["failures"][job] = record["failures"].get(job, 0) + 1
    record.update(extra)
    return record
