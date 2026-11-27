"""Your overrides on the bot's own moves for a gameweek, given in Slack (Phase 3, chosen Nov 27).

  - Skip a planned transfer: the Skip button by each of this week's transfers in the preview. The
    main run then re-plans without it: it keeps the player going out and doesn't buy the one coming
    in, and may make a different move if one still clears the 1-point bar.
  - Pick the captain: the preview's captain menu, or /fpl captain <name>. He has to be in your squad
    and fit (75%+); otherwise the bot's pick stands and the main run says why.
  - Keep a player (/fpl keep <name>): the bot won't sell him. Ban one (/fpl ban <name>): it won't
    buy him.
Each lasts for the coming gameweek only, and can be changed until the main run (about 2 hours
before the deadline). Silence means the bot goes ahead as planned.

Every override is a block in a Slack message whose id is approvals.RECORD's answer-<key>-<choice>-<time>:
worker.js writes one for each tap, and this module for each /fpl command. Keys: skip.<out>.<in>.gw<N>
(on/off), captain.gw<N> (a player id, 0 for the bot's pick), keep.<id>.gw<N> and ban.<id>.gw<N>
(on/off). The latest record for each key, across the channel's recent messages, counts.

python3 overrides.py "keep Saka"   what the Worker starts for /fpl (workflow job slack-command):
                                   keep, ban, captain, explain <name>, or list.
"""
import os
import re
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path

import pandas as pd

import approvals
import data
import doc
import slack

POSITIONS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
TOO_LATE = 2.5 * 3600         # seconds before the deadline: the main run may already have run
HISTORY = 10 * 86400          # how far back the channel is read for this gameweek's overrides
FIT = 75                      # chance of playing needed for your captain pick to stand


@dataclass
class Overrides:
    keep: set = field(default_factory=set)
    ban: set = field(default_factory=set)
    skip: set = field(default_factory=set)        # (out, in) pairs
    captain: int | None = None


def parse(found, gw):
    """Overrides for gameweek gw from {key: (choice, time)} records."""
    out, tag = Overrides(), f"gw{gw}"
    for key, (choice, _) in found.items():
        parts = key.split(".")
        if parts[-1] != tag:
            continue
        if parts[0] == "keep" and len(parts) == 3 and choice == "on":
            out.keep.add(int(parts[1]))
        elif parts[0] == "ban" and len(parts) == 3 and choice == "on":
            out.ban.add(int(parts[1]))
        elif parts[0] == "skip" and len(parts) == 4 and choice == "on":
            out.skip.add((int(parts[1]), int(parts[2])))
        elif parts[0] == "captain" and len(parts) == 2 and choice.isdigit() and int(choice):
            out.captain = int(choice)
    return out


def latest(messages):
    """{key: (choice, time)}: for each key, the latest record in any of the messages."""
    found = {}
    for message in messages:
        for key, (choice, at) in approvals.records(message).items():
            if key not in found or at >= found[key][1]:
                found[key] = (choice, at)
    return found


def channel_messages(now=None):
    """The channel's messages from the last HISTORY seconds (the bot's own posts and confirmations)."""
    now = now or time.time()
    return slack.call("conversations.history", {"channel": os.environ["SLACK_CHANNEL"], "oldest": f"{now - HISTORY:.0f}",
                                                "limit": 200}, form=True)["messages"]


def read(gw):
    """Your overrides for gameweek gw, from Slack (none when Slack isn't set up)."""
    return parse(latest(channel_messages()), gw) if slack.configured() else Overrides()


def settings(base, overrides, gw):
    """The optimizer's settings with your overrides: kept players and the ones a skipped transfer
    sells are locked; banned players and the ones it buys are banned; your captain is fixed."""
    return replace(base, locked=set(base.locked) | overrides.keep | {o for o, _ in overrides.skip},
                   banned=set(base.banned) | overrides.ban | {i for _, i in overrides.skip},
                   captain={**base.captain, gw: overrides.captain} if overrides.captain else base.captain)


def checked(overrides, players, squad, notes):
    """Drops a captain pick that can't stand (not in the squad, or doubtful), saying why in notes."""
    pick = overrides.captain
    if pick is None:
        return overrides
    by_id = players.set_index("id")
    name = by_id.loc[pick, "web_name"] if pick in by_id.index else f"player {pick}"
    chance = by_id.loc[pick, "chance"] if pick in by_id.index else 0      # 100 for an unflagged player
    chance = 0 if chance != chance else chance
    if pick not in squad:
        notes.append(f"Your captain pick, {name}, isn't in your squad, so the bot's pick stands.")
    elif chance < FIT:
        notes.append(f"Your captain pick, {name}, is only {chance:.0f}% to play, so the bot's pick stands.")
    else:
        return overrides
    return replace(overrides, captain=None)


def describe(overrides, players):
    """One line: what you've overridden, or "" for nothing."""
    name = players.set_index("id")["web_name"].to_dict()
    said = [f"keep {name.get(i, i)}" for i in sorted(overrides.keep)]
    said += [f"don't buy {name.get(i, i)}" for i in sorted(overrides.ban)]
    said += [f"skip {name.get(o, o)} → {name.get(i, i)}" for o, i in sorted(overrides.skip)]
    said += [f"captain {name.get(overrides.captain, overrides.captain)}"] if overrides.captain else []
    return "; ".join(said)


def preview_lines(code, gw, transfers, captains, chosen, name):
    """The preview's overrides callout: a Skip / Do it pair by each planned transfer, and the captain
    menu (the bot's pick first, then the next best). transfers: (out, in) pairs; captains: ids, best first."""
    lines = []
    for out, into in transfers:
        key = f"skip.{out}.{into}.gw{gw}"
        lines.append([doc.Tone(f"{name[out]} → {name[into]}", bold=True)])
        lines.append([doc.Button("Skip it", "", "bad", action=f"{code}|{key}|on"),
                      doc.Button("Do it", "", "good", action=f"{code}|{key}|off")])
    if captains:
        options = [(f"{name[i]}{' (the bot’s pick)' if n == 0 else ''}", f"{code}|captain.gw{gw}|{i}")
                   for n, i in enumerate(captains)]
        lines.append([doc.Tone("Captain", bold=True)])
        lines.append([doc.Select("Captain", options, initial=f"{code}|captain.gw{gw}|{chosen or captains[0]}")])
    lines.append(doc.Tone("Or any player, any time before the main run: /fpl keep <name> (won't sell), /fpl ban "
                          "<name> (won't buy), /fpl captain <name>; /fpl list shows them. They last this gameweek.",
                          "muted"))
    return lines


# ---------------------------------------------------------------- /fpl (the slack-command job)

LETTERS = str.maketrans({"Ø": "O", "ø": "o", "Æ": "Ae", "æ": "ae", "Œ": "Oe", "œ": "oe", "Ł": "L", "ł": "l",
                         "Đ": "D", "đ": "d", "ß": "ss", "ı": "i"})     # letters that aren't an accent on another


def plain(text):
    """Lower case without accents: "Ødegaard" -> "odegaard", "Gyökeres" -> "gyokeres"."""
    text = unicodedata.normalize("NFKD", text.translate(LETTERS))
    return "".join(c for c in text if not unicodedata.combining(c)).lower().strip()


def find(name, boot):
    """The players a typed name could mean, best matches first: an exact web name, then full name,
    then any part of the name. A club's short name after it narrows it ("Gabriel ARS")."""
    teams = {t["id"]: t["short_name"] for t in boot["teams"]}
    words = name.split()
    club = words[-1].upper() if len(words) > 1 and words[-1].upper() in teams.values() else None
    wanted = plain(" ".join(words[:-1]) if club else name)
    pool = [p for p in boot["elements"] if club is None or teams[p["team"]] == club]
    for test in (lambda p: plain(p["web_name"]) == wanted,
                 lambda p: plain(f"{p['first_name']} {p['second_name']}") == wanted or plain(p["second_name"]) == wanted,
                 lambda p: wanted in plain(f"{p['web_name']} {p['first_name']} {p['second_name']}")):
        found = [p for p in pool if test(p)]
        if found:
            return sorted(found, key=lambda p: -float(p.get("total_points", 0)))
    return []


def label(p, boot):
    teams = {t["id"]: t["short_name"] for t in boot["teams"]}
    return f"{p['web_name']} ({teams[p['team']]} {POSITIONS[p['element_type']]})"


def confirm(text, key, choice, undo):
    """A confirmation in the channel that is also the record: a block answer-<key>-<choice>-<now>, and
    an Undo button the Worker answers with key|undo."""
    now = int(time.time())
    blocks = [slack.section(text),
              {"type": "context", "block_id": f"answer-{key}-{choice}-{now}",
               "elements": [{"type": "mrkdwn", "text": f"Set {time.strftime('%a %-I:%M %p')} ET. Tap Undo to cancel it."}]},
              slack.actions([slack.button("Undo", f"fpl-{key.rsplit('.', 1)[-1]}|{key}|{undo}")])]
    slack.post(re.sub(r"[*_]", "", text), blocks)


def command(text, boot=None, now=None):
    """Carries out one /fpl command and answers in the channel. Returns True if it did what was asked."""
    boot = boot or data.get("bootstrap-static/")
    now = now or time.time()
    verb, _, rest = text.strip().partition(" ")
    verb, rest = verb.lower(), rest.strip()
    upcoming = next(e for e in boot["events"] if e["is_next"])
    gw, deadline = upcoming["id"], pd.Timestamp(upcoming["deadline_time"]).timestamp()
    if verb == "list":
        found = parse(latest(channel_messages(now)), gw)
        players = pd.DataFrame(boot["elements"])[["id", "web_name"]]
        said = describe(found, players) or "none yet: the bot goes ahead with its own plan"
        slack.post(f"Your overrides for GW{gw}: {said}.")
        return True
    if verb not in ("keep", "ban", "captain", "explain") or not rest:
        slack.post(f"I don't know \"/fpl {text.strip()}\". Try /fpl keep <name>, /fpl ban <name>, /fpl captain <name>, "
                   "/fpl explain <name> or /fpl list.")
        return False
    matches = find(rest, boot)
    if not matches:
        slack.post(f"/fpl {verb}: no player called \"{rest}\". Use the name FPL shows, e.g. Saka or B.Fernandes.")
        return False
    if len(matches) > 1 and plain(matches[0]["web_name"]) != plain(rest):
        options = ", ".join(label(p, boot) for p in matches[:6])
        slack.post(f"/fpl {verb} {rest}: which one? {options}. Add the club to pick one, e.g. \"/fpl {verb} "
                   f"{matches[0]['web_name']} {label(matches[0], boot).split('(')[1].split()[0]}\".")
        return False
    p, who = matches[0], label(matches[0], boot)
    if verb == "explain":
        return explain(p, who)
    if deadline - now < TOO_LATE:
        slack.post(f"Too late for GW{gw}: the main run happens about 2 hours before the deadline. Make the change on the "
                   "FPL site; the final check leaves your changes alone.")
        return False
    if verb == "keep":
        confirm(f"🔒 *Keep {who}* for GW{gw}: the bot won't sell him.", f"keep.{p['id']}.gw{gw}", "on", "off")
    elif verb == "ban":
        confirm(f"🚫 *Don't buy {who}* for GW{gw}.", f"ban.{p['id']}.gw{gw}", "on", "off")
    else:
        confirm(f"©️ *Captain {who}* for GW{gw}, if he's in your squad and fit.", f"captain.gw{gw}", str(p["id"]), "0")
    return True


def explain(p, who):
    """explain.py's breakdown for one player, posted as a message with the detail in its thread."""
    args = [sys.executable, str(Path(__file__).with_name("explain.py")), p["web_name"]]
    if os.getenv("FPL_TEAM_ID"):
        args += ["--team", os.environ["FPL_TEAM_ID"]]
    run = subprocess.run(args, capture_output=True, text=True, timeout=600)
    output = (run.stdout or "") + (f"\n{run.stderr}" if run.returncode else "")
    ts = slack.post(f"Explain: {who}", [slack.section(f"🔎 *Explain: {slack.escape(who)}*: the breakdown is in the thread.")])
    slack.post_first(f"Explain: {who}", [slack.monospace(output.strip() or "(no output)")], thread_ts=ts)
    return run.returncode == 0


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"))
    if len(sys.argv) != 2:
        sys.exit('Usage: python3 overrides.py "keep Saka"')
    sys.exit(0 if command(sys.argv[1]) else 1)
