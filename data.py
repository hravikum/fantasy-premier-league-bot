"""Load FPL data into one shape, either from the live FPL API or from vaastav's season archive.

Every loader returns a Season with the same frames:
  teams     id, code, name, short_name
  players   id, code, web_name, position, team, price, status, chance, news, selected_by
  fixtures  id, event, team_h, team_a, kickoff_time, finished
  history   one row per player per team fixture: the stats the model needs, plus team and
            opponent as stable club codes (team ids are renumbered every season, codes are not)
  events    id, deadline_time, finished   (gameweeks)

Prices are in £m (FPL sends tenths). Times are UTC.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests

API = "https://fantasy.premierleague.com/api/"
ARCHIVE = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data/{season}/{file}"
CACHE = Path(__file__).with_name("cache")
POSITIONS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
HEADERS = {"User-Agent": "Mozilla/5.0 (fpl-bot; personal use)"}

STATS = [
    "minutes", "starts", "goals_scored", "assists", "expected_goals", "expected_assists",
    "expected_goals_conceded", "clean_sheets", "goals_conceded", "saves", "penalties_saved",
    "penalties_missed", "bonus", "bps", "defensive_contribution", "yellow_cards", "red_cards",
    "own_goals", "total_points",
]


@dataclass
class Season:
    name: str
    teams: pd.DataFrame
    players: pd.DataFrame
    fixtures: pd.DataFrame
    history: pd.DataFrame
    events: pd.DataFrame


def previous_season(name):
    start = int(name[:4]) - 1
    return f"{start}-{str(start + 1)[2:]}"


# ---------------------------------------------------------------- shared shaping

def _players(raw):
    p = pd.DataFrame(raw)
    out = pd.DataFrame({
        "id": p["id"], "code": p["code"], "web_name": p["web_name"],
        "position": p["element_type"].map(POSITIONS), "team": p["team"],
        "price": p["now_cost"] / 10, "start_price": (p["now_cost"] - p["cost_change_start"]) / 10,
        "status": p["status"],
        "chance": pd.to_numeric(p["chance_of_playing_next_round"], errors="coerce"),
        "news": p["news"].fillna(""),
        "selected_by": pd.to_numeric(p["selected_by_percent"], errors="coerce"),
        # FPL's set-piece order (1 = first choice); blank if not on the list
        "pen_order": pd.to_numeric(p.get("penalties_order"), errors="coerce"),
    })
    out["chance"] = out["chance"].fillna(out["status"].eq("a") * 100.0)
    return out.dropna(subset=["position"]).reset_index(drop=True)


def _fixtures(raw):
    f = pd.DataFrame(raw)[["id", "event", "team_h", "team_a", "kickoff_time", "finished"]].copy()
    f["kickoff_time"] = pd.to_datetime(f["kickoff_time"], utc=True)
    return f


def _history(rows, fixtures, teams, season):
    """Add team/opponent club codes (from the fixture, so a mid-season transfer is handled)."""
    h = pd.DataFrame(rows)
    h = h.merge(fixtures[["id", "team_h", "team_a"]], left_on="fixture", right_on="id", how="inner")
    h["was_home"] = h["was_home"].astype(str).str.lower().eq("true")
    team_id = h["team_h"].where(h["was_home"], h["team_a"])
    opp_id = h["team_a"].where(h["was_home"], h["team_h"])
    code = teams.set_index("id")["code"]
    h["team_code"] = team_id.map(code)
    h["opp_code"] = opp_id.map(code)
    h["team"] = team_id
    h["kickoff_time"] = pd.to_datetime(h["kickoff_time"], utc=True)
    h["gw"] = h["round"]
    h["season"] = season
    for col in STATS:   # 2024-25 and older have no defensive_contribution: left as NaN, not 0
        h[col] = pd.to_numeric(h[col], errors="coerce").fillna(0) if col in h else float("nan")
    keep = ["season", "element", "fixture", "gw", "kickoff_time", "team", "team_code", "opp_code",
            "was_home", "value"] + STATS + [c for c in ("xP", "position", "code") if c in h]
    return h[keep].sort_values("kickoff_time").reset_index(drop=True)


# ---------------------------------------------------------------- archive (history, backtests)

def _archive_csv(season, file):
    path = CACHE / season / file
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        response = requests.get(ARCHIVE.format(season=season, file=file), headers=HEADERS, timeout=60)
        response.raise_for_status()
        path.write_text(response.text)
    return pd.read_csv(path, low_memory=False)


def load_archive(season):
    raw_players = _archive_csv(season, "players_raw.csv")
    teams = _archive_csv(season, "teams.csv")[["id", "code", "name", "short_name"]]
    fixtures = _fixtures(_archive_csv(season, "fixtures.csv").to_dict("records"))
    gw = _archive_csv(season, "gws/merged_gw.csv")
    gw = gw[gw["position"].isin(POSITIONS.values())]                     # drops 2024-25's managers
    gw = gw.merge(raw_players[["id", "code"]], left_on="element", right_on="id").drop(columns="id")
    history = _history(gw.to_dict("records"), fixtures, teams, season)
    deadlines = fixtures.groupby("event")["kickoff_time"].min() - pd.Timedelta(minutes=90)
    events = deadlines.rename("deadline_time").rename_axis("id").reset_index()
    events["id"] = events["id"].astype(int)
    events["finished"] = True
    players = _players(raw_players.to_dict("records"))
    history["pen_order"] = history["element"].map(players.set_index("id")["pen_order"])
    return Season(season, teams, players, fixtures, history, events)


# ---------------------------------------------------------------- live API (current season)

def get(path, session=None, token=None, **kwargs):
    headers = dict(HEADERS, **({"X-API-Authorization": f"Bearer {token}"} if token else {}))
    response = (session or requests).get(API + path, headers=headers, timeout=30, **kwargs)
    response.raise_for_status()
    return response.json()


def post(path, token, payload):
    """Send a change to FPL the way the website does. Returns the response; the caller checks it."""
    headers = dict(HEADERS, **{"X-API-Authorization": f"Bearer {token}", "Content-Type": "application/json",
                               "Origin": "https://fantasy.premierleague.com",
                               "Referer": "https://fantasy.premierleague.com/"})
    return requests.post(API + path, json=payload, headers=headers, timeout=30)


def load_live():
    """Current season from the public API. Per-match history needs one call per player, so it
    is only fetched for players who have played this season."""
    session = requests.Session()
    boot = get("bootstrap-static/", session)
    season_start = pd.to_datetime(boot["events"][0]["deadline_time"]).year
    name = f"{season_start}-{str(season_start + 1)[2:]}"
    teams = pd.DataFrame(boot["teams"])[["id", "code", "name", "short_name"]]
    players = _players(boot["elements"])
    fixtures = _fixtures(get("fixtures/", session))
    events = pd.DataFrame(boot["events"])[["id", "deadline_time", "finished"]]
    events["deadline_time"] = pd.to_datetime(events["deadline_time"], utc=True)

    played = [e["id"] for e in boot["elements"] if e["minutes"] > 0]

    def summary(element):
        for attempt in range(3):
            try:
                return get(f"element-summary/{element}/", session)["history"]
            except requests.RequestException:
                if attempt == 2:
                    raise
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = [row for result in pool.map(summary, played) for row in result]
    # Players who haven't played get a zero-minute row for each of their team's finished
    # fixtures, so the minutes model sees "not playing" instead of falling back on last season.
    done = fixtures[fixtures["finished"]]
    for player in players[~players["id"].isin(played)].itertuples():
        for fx in done[(done.team_h == player.team) | (done.team_a == player.team)].itertuples():
            rows.append({"element": player.id, "fixture": fx.id, "round": fx.event,
                         "was_home": fx.team_h == player.team, "kickoff_time": fx.kickoff_time,
                         "value": player.price * 10})
    history = _history(rows, fixtures, teams, name)
    by_id = players.set_index("id")
    history["code"] = history["element"].map(by_id["code"])
    history["position"] = history["element"].map(by_id["position"])
    history["pen_order"] = history["element"].map(by_id["pen_order"])
    return Season(name, teams, players, fixtures, history, events)


def load_with_prior(current):
    """The current season plus last season's archive, for the model's priors."""
    prior = load_archive(previous_season(current.name))
    return pd.concat([prior.history, current.history], ignore_index=True)
