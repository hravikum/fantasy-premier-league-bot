"""Team news from the Claude news check.

The preview writes state/watchlist.json (kept on bot-state): the players this week's plan depends
on, with FPL's own flags. A scheduled Claude task reads it a few hours before the deadline, reads
press-conference reports and predicted lineups, and writes news.json to the bot-news branch: a
chance to start, a reason and a source for each watchlist player whose news says more than FPL's
flags do. It also writes heartbeat.json every time it runs, even when there was nothing to do.

hosted_run.sh fetches both files. The main run and final check use news.json for the coming
gameweek only, if it's from the last 20 hours, and every email lists what it changed.
"""
import json
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent
WATCHLIST = HERE / "state" / "watchlist.json"
NEWS = HERE / "news.json"
HEARTBEAT = HERE / "heartbeat.json"
FRESH = pd.Timedelta(hours=20)
STALE_CHECK = pd.Timedelta(hours=26)       # the check runs every 6 hours; this long means it stopped


def eastern(stamp):
    return pd.Timestamp(stamp).tz_convert("America/New_York").strftime("%a %d %b %H:%M")


def write_watchlist(gw, deadline, ids, players, teams):
    by_id = players.set_index("id")
    team_name = teams.set_index("id")["name"]
    rows = [{"id": int(i), "name": by_id.loc[i, "web_name"], "team": team_name[by_id.loc[i, "team"]],
             "position": by_id.loc[i, "position"], "fpl_status": by_id.loc[i, "status"],
             "fpl_chance": None if pd.isna(by_id.loc[i, "chance"]) else float(by_id.loc[i, "chance"]),
             "fpl_news": by_id.loc[i, "news"]}
            for i in dict.fromkeys(ids) if i in by_id.index]
    WATCHLIST.parent.mkdir(exist_ok=True)
    WATCHLIST.write_text(json.dumps({"gw": gw, "deadline": deadline.isoformat(), "players": rows}, indent=1))


def load(gw, now):
    """Start-chance overrides {player id: 0..1} for this gameweek, and lines for the email."""
    lines, overrides = [], {}
    beat = json.loads(HEARTBEAT.read_text()) if HEARTBEAT.exists() else None
    if beat is None:
        lines.append("WARNING: the Claude news check has never reported in.")
    elif now - pd.Timestamp(beat["last_run"]) > STALE_CHECK:
        lines.append(f"WARNING: the Claude news check last ran {eastern(beat['last_run'])}; it may have stopped.")
    news = json.loads(NEWS.read_text()) if NEWS.exists() else None
    if not news or news.get("gw") != gw or now - pd.Timestamp(news["checked_at"]) > FRESH:
        lines.insert(0, f"TEAM NEWS: no Claude news check for GW{gw} yet; using FPL's flags only.")
        return overrides, lines
    lines.insert(0, f"TEAM NEWS (Claude check, {eastern(news['checked_at'])})")
    for player in news.get("players", []):
        chance = player.get("start_chance")
        if not isinstance(chance, (int, float)) or not 0 <= chance <= 1:
            continue
        overrides[int(player["id"])] = float(chance)
        lines.append(f"  {player.get('name', player['id'])}: {chance:.0%} to start. {player.get('reason', '')}"
                     + (f" ({player['source']})" if player.get("source") else ""))
    if not overrides:
        lines.append("  Nothing beyond FPL's own flags.")
    return overrides, lines
