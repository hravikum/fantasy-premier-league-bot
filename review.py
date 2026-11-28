"""After each gameweek: how you, the bot and the model did, and how The Slash stands.

The tick (bot.py) runs it once FPL has confirmed the gameweek's points, bonus included (the event's
data_checked), for each gameweek since the bot went live (FIRST_GW). Public data only, no login.

  You vs the bot   your team as it played against the lineup the bot set (at its main run, or its final
                   check if that changed it), both scored the way FPL does: automatic subs in bench order
                   keeping a valid formation, the vice-captain if the captain didn't play, hits taken off.
  The model        the bot's projections against what its lineup scored: the XI, the captain, and each
                   transfer it made (who came in against who went).
  The Slash        the standings, and each rival's captain, chip and transfers this gameweek.
  Hindsight        your answers to hits, chips and chip weeks, and your overrides, against what happened.
  Season           running totals (state/reviews.json).

The main run and final check keep the bot's lineup in state/lineups.json (remember). GW12 was played
before that: its lineup comes from the write log (state/writes.log), without projections or decisions.

python3 review.py 12     the review for GW12, printed (nothing sent)
"""
import json
import os
import sys
import traceback
from pathlib import Path

import pandas as pd

import data
import doc

STATE = Path(__file__).with_name("state")
LINEUPS = STATE / "lineups.json"
REVIEWS = STATE / "reviews.json"
WRITES = STATE / "writes.log"
FIRST_GW = 12                      # the first gameweek the bot played live (BOT_LIVE from 26 Nov 2026)
LEAGUE = "The Slash"               # your mini-league (your choice, Nov 27)
MAX_RIVALS = 20                    # rivals whose captain, chip and transfers are looked up
MAX_TRIES = 6                      # failed reviews of one gameweek before it says so and stops trying
FORMATION = {"GK": (1, 1), "DEF": (3, 5), "MID": (2, 5), "FWD": (1, 3)}
POSITIONS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}


def _load(path):
    return json.loads(path.read_text()) if path.exists() else {}


def _save(path, value):
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(value, indent=1))


# ---------------------------------------------------------------- the bot's lineups

def remember(gw, picks, chip=None, source="main run", **more):
    """Keep the lineup the bot set for this gameweek (picks: element, position, is_captain,
    is_vice_captain), with what the review needs from the main run: xp {element: projection},
    transfers [[out, in]], hits, decisions (with your answers), overrides, bot_captain."""
    kept = _load(LINEUPS)
    entry = kept.get(str(gw), {}) if source != "main run" else {}
    entry.update(picks=[{k: p[k] for k in ("element", "position", "is_captain", "is_vice_captain")} for p in picks],
                 chip=chip, source=source, **more)
    kept[str(gw)] = entry
    _save(LINEUPS, kept)


def from_log(gw, events):
    """The bot's lineup for a gameweek played before lineups were kept: the last lineup it sent
    between the previous deadline and this one (state/writes.log)."""
    if not WRITES.exists():
        return None
    deadlines = events.set_index("id")["deadline_time"]
    start = deadlines.get(gw - 1, pd.Timestamp.min.tz_localize("UTC"))
    found = None
    for line in WRITES.read_text().splitlines():
        entry = json.loads(line)
        at = pd.Timestamp(entry["at"])
        if entry.get("kind") == "lineup" and entry.get("outcome") == "sent" and start <= at < deadlines[gw]:
            found = {"picks": entry["payload"]["picks"], "chip": entry["payload"].get("chip"),
                     "source": f"{entry['job']} run (write log)"}
    return found


def lineup(gw, events):
    return _load(LINEUPS).get(str(gw)) or from_log(gw, events)


# ---------------------------------------------------------------- scoring as FPL does

def score(picks, live, positions, chip=None):
    """Points for a team as FPL scores it, before hits. live: {element: (minutes, points)}.
    Returns (points, the 11 (or 15) who counted, automatic subs [(out, in)], who got the armband)."""
    played = lambda e: live.get(e, (0, 0))[0] > 0
    points = lambda e: live.get(e, (0, 0))[1]
    order = sorted(picks, key=lambda p: p["position"])
    xi = [p["element"] for p in order if p["position"] <= 11]
    bench = [p["element"] for p in order if p["position"] > 11]
    subs = []
    if chip == "bboost":
        counted = xi + bench
    else:
        counted = list(xi)
        for b in (b for b in bench if played(b)):
            for out in (e for e in counted if not played(e)):
                trial = [b if e == out else e for e in counted]
                count = pd.Series([positions[e] for e in trial]).value_counts()
                if all(lo <= count.get(pos, 0) <= hi for pos, (lo, hi) in FORMATION.items()):
                    counted, subs = trial, subs + [(out, b)]
                    break
    captain = next((p["element"] for p in picks if p["is_captain"]), None)
    vice = next((p["element"] for p in picks if p["is_vice_captain"]), None)
    armband = captain if captain is not None and played(captain) else vice if vice is not None and played(vice) else None
    extra = (2 if chip == "3xc" else 1) * points(armband) if armband is not None else 0
    return sum(points(e) for e in counted) + extra, counted, subs, armband


# ---------------------------------------------------------------- when

def latest(events):
    """The latest gameweek, from FIRST_GW, whose points FPL has confirmed (bonus in), or None."""
    confirmed = events.get("data_checked", pd.Series(False, index=events.index)).fillna(False).astype(bool)
    checked = events[(events["id"] >= FIRST_GW) & confirmed]
    return None if checked.empty else int(checked["id"].max())


def due(events):
    """The gameweek to review now, or None: the latest confirmed one, if not reviewed yet and not
    given up on."""
    reviews = _load(REVIEWS)
    gw = latest(events)
    if gw is None or gw in reviews.get("done", []):
        return None
    return None if reviews.get("failures", {}).get(str(gw), 0) >= MAX_TRIES else gw


def mark(gw, ok, row=None, problem=""):
    reviews = _load(REVIEWS)
    if ok:
        reviews["done"] = sorted(set(reviews.get("done", [])) | {gw})
        reviews.setdefault("season", {})[str(gw)] = row or {}
    else:
        failures = reviews.setdefault("failures", {})
        failures[str(gw)] = failures.get(str(gw), 0) + 1
    _save(REVIEWS, reviews)
    return reviews.get("failures", {}).get(str(gw), 0)


# ---------------------------------------------------------------- the review

class Names:
    def __init__(self, boot):
        self.web = {p["id"]: p["web_name"] for p in boot["elements"]}
        self.position = {p["id"]: POSITIONS[p["element_type"]] for p in boot["elements"]}

    def __call__(self, element):
        return self.web.get(element, f"#{element}")


def _signed(x):
    return f"{x:+.0f}" if x == round(x) else f"{x:+.1f}"


def you_vs_bot(gw, mine, bot, live, names, cost, bot_hits):
    """Blocks and the two scores (yours net of hits, the bot's net of its hits)."""
    yours_raw, your_xi, your_subs, your_arm = score(mine["picks"], live, names.position, mine.get("active_chip"))
    blocks = [doc.Heading(f"You vs the bot, GW{gw}")]
    if bot is None:
        return blocks + [doc.Para("The bot's lineup for this gameweek wasn't kept, so there's nothing to compare.",
                                  tone="muted")], None, None
    bots_raw, bot_xi, bot_subs, bot_arm = score(bot["picks"], live, names.position, bot.get("chip"))
    you, them = mine["points"] - cost, bots_raw - 4 * bot_hits
    pts = lambda e: live.get(e, (0, 0))[1]
    lines = [[doc.Tone(f"Your team: {you} points", bold=True), f" (FPL's score{f', after -{cost} in hits' if cost else ''})."],
             [doc.Tone(f"The bot's lineup: {them} points", bold=True),
              f" (as its {bot.get('source', 'main run')} left it{f', after -{4 * bot_hits} in hits' if bot_hits else ''})."]]
    if yours_raw != mine["points"]:
        lines.append(doc.Tone(f"(Scored the bot's way, your team makes {yours_raw}: FPL's {mine['points']} counts.)", "muted"))
    same = sorted(map(_key, mine["picks"])) == sorted(map(_key, bot["picks"])) and mine.get("active_chip") == bot.get("chip")
    if same:
        lines.append("You kept the bot's lineup.")
    else:
        diff = you - them
        lines.append([doc.Tone(f"Your changes: {_signed(diff)}", "good" if diff > 0 else "bad" if diff < 0 else None,
                               bold=True), " compared with leaving the bot's lineup."])
        squad_mine = {p["element"] for p in mine["picks"]}
        squad_bot = {p["element"] for p in bot["picks"]}
        if squad_mine != squad_bot:
            ins, outs = sorted(squad_mine - squad_bot), sorted(squad_bot - squad_mine)
            lines.append("Your squad differed (a transfer after the bot's run): yours had "
                         + ", ".join(f"{names(e)} {pts(e)}" for e in ins) + "; the bot's had "
                         + ", ".join(f"{names(e)} {pts(e)}" for e in outs) + ".")
        started_mine = [e for e in your_xi if e not in bot_xi]
        started_bot = [e for e in bot_xi if e not in your_xi]
        if started_mine or started_bot:
            lines.append("Counted for you, not the bot: " + (", ".join(f"{names(e)} {pts(e)}" for e in started_mine) or "nobody")
                         + ". For the bot, not you: " + (", ".join(f"{names(e)} {pts(e)}" for e in started_bot) or "nobody") + ".")
        if your_arm != bot_arm:
            arm = lambda e, chip: f"{names(e)} {pts(e)} ×{3 if chip == '3xc' else 2}" if e else "nobody (neither played)"
            lines.append(f"Armband: yours {arm(your_arm, mine.get('active_chip'))}; the bot's {arm(bot_arm, bot.get('chip'))}.")
        if mine.get("active_chip") != bot.get("chip"):
            lines.append(f"Chip: yours {mine.get('active_chip') or 'none'}; the bot's {bot.get('chip') or 'none'}.")
    for who, subs in (("Your", your_subs), ("The bot's", bot_subs if not same else [])):
        if subs:
            lines.append(f"{who} automatic subs: " + ", ".join(f"{names(o)} → {names(i)} {pts(i)}" for o, i in subs) + ".")
    return blocks + [doc.Para(line) for line in lines], you, them


def _key(p):
    return (int(p["element"]), int(p["position"]), bool(p["is_captain"]), bool(p["is_vice_captain"]))


def model_check(gw, bot, live, names):
    """Blocks, and (projected, scored) for the bot's lineup, or (None, None) without projections."""
    blocks = [doc.Heading("The model")]
    xp = {int(k): v for k, v in (bot or {}).get("xp", {}).items()}
    if not xp:
        return blocks + [doc.Para("No projections were kept for this gameweek (they're kept from GW13 on).",
                                  tone="muted")], None, None
    pts = lambda e: live.get(e, (0, 0))[1]
    order = sorted(bot["picks"], key=lambda p: p["position"])
    xi = [p["element"] for p in order if p["position"] <= 11]
    captain = next(p["element"] for p in order if p["is_captain"])
    chip = bot.get("chip")
    counted = xi + ([p["element"] for p in order if p["position"] > 11] if chip == "bboost" else [])
    mult = 3 if chip == "3xc" else 2
    projected = sum(xp.get(e, 0) for e in counted) + (mult - 1) * xp.get(captain, 0)
    actual = sum(pts(e) for e in counted) + (mult - 1) * pts(captain)
    rows = sorted(([names(e), f"{xp.get(e, 0):.1f}", str(pts(e)), _signed(pts(e) - xp.get(e, 0))] for e in counted),
                  key=lambda r: -abs(float(r[3])))
    lines = [[doc.Tone(f"Projected {projected:.1f}, scored {actual}", bold=True),
              f" (the bot's XI{' and bench' if chip == 'bboost' else ''}, captain counted {mult}×, before any "
              "automatic subs)."],
             f"Captain {names(captain)}: projected {xp.get(captain, 0):.1f}, scored {pts(captain)}."]
    biggest = rows[:3]
    lines.append("Biggest surprises: " + ", ".join(f"{r[0]} {r[2]} (projected {r[1]})" for r in biggest) + ".")
    for out, into in bot.get("transfers", []):
        lines.append(f"Transfer {names(out)} → {names(into)}: {names(into)} scored {pts(into)} (projected "
                     f"{xp.get(into, 0):.1f}), {names(out)} {pts(out)} (projected {xp.get(out, 0):.1f}): "
                     f"{_signed(pts(into) - pts(out))} this week.")
    blocks += [doc.Para(line) for line in lines]
    blocks.append(doc.Table(["Player", "Projected", "Scored", "Diff"], rows, right={1, 2, 3},
                            slack=[[doc.Tone(r[0], bold=True), f"  {r[2]} (projected {r[1]})"] for r in rows]))
    return blocks, round(projected, 1), actual


def the_slash(gw, entry_id, names, get=None):
    """Blocks, and (your rank, entries) in the league, or (None, None) if it isn't found."""
    get = get or data.get
    blocks = [doc.Heading(f"Mini-league: {LEAGUE}, after GW{gw}")]
    leagues = get(f"entry/{entry_id}/").get("leagues", {}).get("classic", [])
    league = next((l for l in leagues if l["name"].strip().lower() == LEAGUE.lower()), None)
    if league is None:
        return blocks + [doc.Para(f"Couldn't find {LEAGUE} among your leagues.", tone="muted")], None, None
    standings = get(f"leagues-classic/{league['id']}/standings/")["standings"]
    results = standings["results"]
    rows, slack_rows, captains, chips_played, moves = [], [["#", "Team", "GW", "Total", "Captain"]], {}, [], []
    me = None
    for r in results:
        captain, chip = "", None
        if len(rows) < MAX_RIVALS or r["entry"] == entry_id:
            picks = get(f"entry/{r['entry']}/event/{gw}/picks/")
            cap = next((p["element"] for p in picks["picks"] if p["is_captain"]), None)
            captain, chip = names(cap) if cap else "", picks.get("active_chip")
            captains[captain] = captains.get(captain, 0) + 1
            if r["entry"] != entry_id:
                made = [t for t in get(f"entry/{r['entry']}/transfers/") if t["event"] == gw]
                if made:
                    cost = picks.get("entry_history", {}).get("event_transfers_cost", 0)
                    moves.append(f"{r['entry_name']}: " + ", ".join(f"{names(t['element_out'])} → {names(t['element_in'])}"
                                                                   for t in made) + (f" (-{cost})" if cost else ""))
            if chip:
                chips_played.append(f"{r['entry_name']}: {CHIP_NAMES.get(chip, chip)}")
        if r["entry"] == entry_id:
            me = r
        change = (r.get("last_rank") or r["rank"]) - r["rank"]
        moved = f" ↑{change}" if change > 0 else f" ↓{-change}" if change < 0 else ""
        team = r["entry_name"] + (" (you)" if r["entry"] == entry_id else "")
        rows.append([f"{r['rank']}{moved}", team, str(r["event_total"]), str(r["total"]), captain])
        slack_rows.append([f"{r['rank']}{moved}", team, str(r["event_total"]), str(r["total"]), captain])
    lines = []
    if me:
        best = max(results, key=lambda r: r["event_total"])
        lines.append([doc.Tone(f"You're {_ordinal(me['rank'])} of {len(results)}", bold=True),
                      f" (after GW{gw - 1}: {_ordinal(me.get('last_rank') or me['rank'])}). This week you scored "
                      f"{me['event_total']}; {best['entry_name']} had the most, {best['event_total']}."])
    if captains:
        top, count = max(captains.items(), key=lambda kv: kv[1])
        lines.append(f"Most captained: {top} ({count} of {sum(captains.values())}).")
    lines.append("Chips played: " + ("; ".join(chips_played) if chips_played else "none") + ".")
    if moves:
        lines.append("Rivals' transfers: " + "; ".join(moves) + ".")
    if standings.get("has_next"):
        lines.append(doc.Tone(f"Only the top {len(results)} are shown.", "muted"))
    blocks += [doc.Para(line) for line in lines]
    blocks.append(doc.Table(["#", "Team", "GW", "Total", "Captain"], rows, right={2, 3}, slack_rows=slack_rows))
    return blocks, (me["rank"] if me else None), len(results)


CHIP_NAMES = {"wildcard": "Wildcard", "freehit": "Free Hit", "bboost": "Bench Boost", "3xc": "Triple Captain"}


def _ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def hindsight(bot, mine, live, names):
    """Your answers and overrides this gameweek against what happened."""
    blocks = [doc.Heading("Hindsight")]
    pts = lambda e: live.get(e, (0, 0))[1]
    lines = []
    for d in (bot or {}).get("decisions", []):
        answer = {"yes": "you said yes", "no": "you said no"}.get(d.get("answer"), "no answer, so no")
        if d["kind"] == "transfers":
            gain = sum(pts(i) for i in d["in"]) - sum(pts(o) for o in d["out"])
            moves = ", ".join(f"{names(o)} {pts(o)} → {names(i)} {pts(i)}" for o, i in zip(d["out"], d["in"]))
            made = "made" if d.get("answer") == "yes" else "would have made"
            lines.append(f"{d['hits']} hit{'s' if d['hits'] > 1 else ''} for {moves}: {answer}. This week they "
                         f"{made} {_signed(gain - 4 * d['hits'])} after the hit (and they carry on into later weeks).")
        elif d["kind"] == "chip":
            chip, yes = d["chip"], d.get("answer") == "yes"
            if chip in ("bboost", "3xc"):       # the same team scored with the chip and without it
                picks = (mine if yes else bot)["picks"]
                gain = score(picks, live, names.position, chip)[0] - score(picks, live, names.position)[0]
                worth = f"it {'added' if yes else 'would have added'} {gain}"
            else:
                worth = "there's no telling what another squad would have made"
            lines.append(f"{CHIP_NAMES.get(chip, chip)}: {answer}; {worth}.")
        elif d["kind"] == "commit":
            lines.append(f"Plan around {CHIP_NAMES.get(d['chip'], d['chip'])} in GW{d['gw']}: {answer}.")
    said = (bot or {}).get("overrides") or {}
    if said.get("captain"):
        own = said.get("bot_captain")
        lines.append(f"Your captain {names(said['captain'])} scored {pts(said['captain'])}"
                     + (f"; the bot's pick, {names(own)}, scored {pts(own)}." if own else "."))
    for out, into in said.get("skip", []):
        lines.append(f"You skipped {names(out)} → {names(into)}: {names(into)} scored {pts(into)}, "
                     f"{names(out)} {pts(out)}.")
    for e in said.get("keep", []):
        lines.append(f"You kept {names(e)}: {pts(e)} points.")
    for e in said.get("ban", []):
        lines.append(f"You told the bot not to buy {names(e)}: he scored {pts(e)}.")
    if not lines:
        lines = [doc.Tone("No decisions or overrides this gameweek." if bot and "decisions" in bot
                          else "Your answers weren't kept for this gameweek (they're kept from GW13 on).", "muted")]
    return blocks + [doc.Para(line) for line in lines]


def season(rows):
    """Running totals over the reviewed gameweeks (rows: {gw: {...}}), including this one."""
    blocks = [doc.Heading("Season so far")]
    compared = [r for r in rows.values() if r.get("you") is not None and r.get("bot") is not None]
    lines = []
    if compared:
        diff = sum(r["you"] - r["bot"] for r in compared)
        better = sum(r["you"] > r["bot"] for r in compared)
        worse = sum(r["you"] < r["bot"] for r in compared)
        lines.append(f"Your changes vs the bot's lineups: {_signed(diff)} over {len(compared)} gameweek"
                     f"{'s' if len(compared) > 1 else ''} ({better} better, {worse} worse, "
                     f"{len(compared) - better - worse} the same).")
    modelled = [r for r in rows.values() if r.get("projected") is not None]
    if modelled:
        lines.append(f"The model: projected {sum(r['projected'] for r in modelled) / len(modelled):.1f} a week for "
                     f"the bot's lineup, which scored {sum(r['scored'] for r in modelled) / len(modelled):.1f} "
                     f"({len(modelled)} gameweek{'s' if len(modelled) > 1 else ''}).")
    ranks = [(int(gw), r) for gw, r in sorted(rows.items(), key=lambda kv: int(kv[0])) if r.get("league_rank")]
    if ranks:
        lines.append(f"{LEAGUE}: " + ", ".join(f"GW{gw} {_ordinal(r['league_rank'])}" for gw, r in ranks) + ".")
    return blocks + [doc.Para(line) for line in lines or [doc.Tone("Nothing to total yet.", "muted")]]


def build(gw, entry_id, get=None):
    """The review as a doc.Message, and the season row it adds."""
    get = get or data.get
    boot = get("bootstrap-static/")
    names = Names(boot)
    events = pd.DataFrame(boot["events"])
    events["deadline_time"] = pd.to_datetime(events["deadline_time"], utc=True)
    event = next(e for e in boot["events"] if e["id"] == gw)
    live = {e["id"]: (e["stats"]["minutes"], e["stats"]["total_points"]) for e in get(f"event/{gw}/live/")["elements"]}
    picks = get(f"entry/{entry_id}/event/{gw}/picks/")
    history = picks["entry_history"]
    mine = {"picks": picks["picks"], "active_chip": picks.get("active_chip"), "points": history["points"]}
    cost = history.get("event_transfers_cost", 0)
    bot = lineup(gw, events)
    bot_hits = (bot or {}).get("hits", 0)

    blocks_vs, you, them = you_vs_bot(gw, mine, bot, live, names, cost, bot_hits)
    blocks_model, projected, scored = model_check(gw, bot, live, names)
    try:
        blocks_league, rank, size = the_slash(gw, entry_id, names, get)
    except Exception as problem:            # the rest of the review still goes out
        blocks_league, rank, size = [doc.Heading(f"Mini-league: {LEAGUE}"), doc.Para(
            f"Couldn't read {LEAGUE} ({type(problem).__name__}: {problem}).", tone="muted")], None, None
    row = {"points": history["points"], "you": you, "bot": them, "projected": projected, "scored": scored,
           "league_rank": rank, "league_size": size, "overall_rank": history.get("overall_rank")}
    rows = {**_load(REVIEWS).get("season", {}), str(gw): row}

    previous = next((h for h in get(f"entry/{entry_id}/history/")["current"] if h["event"] == gw - 1), {})
    before, after = previous.get("overall_rank"), history.get("overall_rank")
    top = [f"{history['points']} points" + (f" ({cost} in hits taken off: {history['points'] - cost})" if cost else "")
           + f". Average {event.get('average_entry_score', '?')}, highest {event.get('highest_score', '?')}."]
    if after:
        top.append(f"Overall rank {after:,}" + (f", from {before:,}" if before else "") + ".")
    if rank:
        top.append(f"{LEAGUE}: {_ordinal(rank)} of {size}.")
    if you is not None and them is not None and you != them:
        top.append(f"Your changes vs the bot's lineup: {_signed(you - them)}.")
    blocks = [doc.Callout(f"GW{gw} at a glance", top, "info")] + blocks_vs + blocks_model + blocks_league + \
        hindsight(bot, mine, live, names) + season(rows)
    footer = (f"Run: {os.getenv('GITHUB_SERVER_URL')}/{os.getenv('GITHUB_REPOSITORY')}/actions/runs/"
              f"{os.getenv('GITHUB_RUN_ID')}" if os.getenv("GITHUB_RUN_ID") else "(local run)")
    message = doc.Message(f"GW{gw} review", f"{history['points']} points" + (f" · {_ordinal(rank)} in {LEAGUE}" if rank else ""),
                          blocks=blocks, footer=footer)
    return message, row


def run(gw, entry_id, send=True):
    """Make the review, send it (Slack, and email while EMAIL_TOO), and mark it done. Never raises:
    a failure is counted, retried at the next tick, and after MAX_TRIES said once."""
    import mailer
    try:
        message, row = build(gw, entry_id)
    except Exception as problem:
        traceback.print_exc()
        tries = mark(gw, False)
        if send and tries >= MAX_TRIES:
            mailer.send(f"GW{gw} review: couldn't be made", f"{type(problem).__name__}: {problem}. The bot stops "
                        f"trying for GW{gw}; nothing else is affected.")
        return False
    print(doc.to_text(message))
    if send:
        subject = f"GW{gw} review: {message.subtitle}"
        mailer.send(subject, doc.to_text(message), doc.to_html(message), slack_posts=doc.to_slack(message))
        mark(gw, True, row)
    return True


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"))
    if len(sys.argv) != 2 or not sys.argv[1].isdigit() or not os.getenv("FPL_TEAM_ID"):
        sys.exit("Usage: FPL_TEAM_ID=1234567 python3 review.py 12")
    sys.exit(0 if run(int(sys.argv[1]), int(os.environ["FPL_TEAM_ID"]), send=False) else 1)
