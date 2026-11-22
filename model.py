"""Expected FPL points per player per gameweek.

For each fixture a player's expected points are built from FPL's own scoring rules:
  appearance       P(start) x P(60+ | start), and sub appearances
  goals, assists   per-90 non-penalty xG and xA, shrunk toward a price-based prior, scaled by the
                   opponent's defence and home advantage, then calibrated to FPL's goals/assists
  penalties        the team's penalty chances go to the first taker on FPL's list who is likely to
                   be on the pitch (so a stand-in inherits them when the regular taker is out)
  clean sheets     Poisson: P(opponent scores 0) from team attack/defence ratings fitted on xG
  goals conceded   -1 per 2 (GK/DEF), Poisson
  saves            +1 per 3 (GK), Poisson
  def. contrib.    +2 at 10 actions (DEF) / 12 (MID/FWD), negative binomial on the player's rate
  bonus, cards     per-90 rates; bonus scaled by how good the fixture is
Everything is multiplied by availability from FPL's injury status and news.
"""
import re
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.stats import nbinom, poisson

GOAL_PTS = {"GK": 10, "DEF": 6, "MID": 5, "FWD": 4}
CS_PTS = {"GK": 4, "DEF": 4, "MID": 1, "FWD": 0}
DEFCON_AT = {"GK": 999, "DEF": 10, "MID": 12, "FWD": 12}
RATES = ["np_xg", "expected_assists", "defensive_contribution", "saves", "bonus", "yellow_cards"]
PENALTIES_PER_TEAM_MATCH = 0.09    # league average, scaled by how much a team attacks in the fixture
PENALTY_XG = 0.79                  # Opta's xG for a penalty, also roughly the share scored


@dataclass
class Params:
    half_life_days: float = 365.0      # how fast old matches fade for rates and team ratings
    prior_minutes: float = 300.0       # weight of the price-based prior, in minutes played
    team_prior_games: float = 6.0      # weight of the team-rating prior, in matches
    promoted_attack: float = 0.80      # prior for a team with no Premier League data
    promoted_defence: float = 1.25
    minutes_half_life: float = 1.5     # matches; recency weighting for the minutes model
    defcon_dispersion: float = 12.0    # negative-binomial size; lower = more match-to-match variance
    # Highest chance to start, by position, for a player who has started every recent match. The
    # minutes model alone tops out near 0.89 (it has to allow for injuries it can't see); live runs
    # also know FPL's injury flags, so for unflagged regulars it is raised to these. Last season,
    # regulars started their next match 88% of the time (keepers 95%), injuries included.
    # None = leave the minutes model alone (backtests, which have no flags).
    start_ceiling: dict | None = None


def decay(times, as_of, half_life_days):
    age = (as_of - times).dt.total_seconds() / 86400
    return 0.5 ** (age.clip(lower=0) / half_life_days)


# ---------------------------------------------------------------- team strength

def team_ratings(history, as_of, current_codes, p):
    """Attack and defence multipliers per club code, fitted to xG with a Poisson-style
    multiplicative model: xG(i vs j) = mu * attack_i * defence_j * home."""
    rows = history[history["kickoff_time"] < as_of]
    m = (rows.groupby(["season", "fixture", "team_code", "opp_code", "was_home", "kickoff_time"],
                      as_index=False)["expected_goals"].sum())
    m["w"] = decay(m["kickoff_time"], as_of, p.half_life_days)
    mu = np.average(m["expected_goals"], weights=m["w"]) if len(m) else 1.4
    home_xg = np.average(m.loc[m.was_home, "expected_goals"], weights=m.loc[m.was_home, "w"]) if m.was_home.any() else mu
    away_xg = np.average(m.loc[~m.was_home, "expected_goals"], weights=m.loc[~m.was_home, "w"]) if (~m.was_home).any() else mu
    home = np.sqrt(home_xg / away_xg)
    m["hf"] = np.where(m["was_home"], home, 1 / home)

    seen = set(rows.loc[rows["season"] != rows["season"].max(), "team_code"]) if len(rows) else set()
    codes = sorted(set(m["team_code"]) | set(current_codes))
    promoted = {c for c in current_codes if c not in seen and seen}
    prior_a = pd.Series({c: p.promoted_attack if c in promoted else 1.0 for c in codes})
    prior_d = pd.Series({c: p.promoted_defence if c in promoted else 1.0 for c in codes})
    a, d = prior_a.copy(), prior_d.copy()
    k = p.team_prior_games
    for _ in range(60):
        exp_for = (m["w"] * mu * m["opp_code"].map(d) * m["hf"]).groupby(m["team_code"]).sum()
        got_for = (m["w"] * m["expected_goals"]).groupby(m["team_code"]).sum()
        a = (got_for.reindex(codes, fill_value=0) + k * mu * prior_a) / (exp_for.reindex(codes, fill_value=0) + k * mu)
        exp_against = (m["w"] * mu * m["team_code"].map(a) * m["hf"]).groupby(m["opp_code"]).sum()
        got_against = (m["w"] * m["expected_goals"]).groupby(m["opp_code"]).sum()
        d = (got_against.reindex(codes, fill_value=0) + k * mu * prior_d) / (exp_against.reindex(codes, fill_value=0) + k * mu)
    return {"mu": mu, "home": home, "attack": a, "defence": d}


# ---------------------------------------------------------------- player rates

def _price_priors(rows):
    """Per position, per-90 rate as a straight line in price, fitted on player-seasons with 450+ min."""
    rows = rows[rows["minutes"] > 0]
    ps = rows.groupby(["season", "code"]).agg(
        position=("position", "last"), price=("value", "mean"), minutes=("minutes", "sum"),
        **{s: (s, lambda x: x.sum(min_count=1)) for s in RATES})
    ps = ps[ps["minutes"] >= 450]
    ps["price"] = ps["price"] / 10
    fits = {}
    for pos, g_all in ps.groupby("position"):
        for stat in RATES:
            g = g_all.dropna(subset=[stat])     # seasons that didn't record the stat sit out
            per90 = g[stat] / g["minutes"] * 90
            if len(g) >= 5:
                slope, intercept = np.polyfit(g["price"], per90, 1, w=np.sqrt(g["minutes"]))
            else:
                slope, intercept = 0.0, float(per90.mean()) if len(g) else 0.0
            fits[(pos, stat)] = (intercept, slope)
    return fits


def non_penalty_xg(rows):
    """xG minus an estimate of penalty xG. FPL's stats don't say which xG came from penalties, so
    each team-match's expected penalties are charged to the first-choice taker (FPL's order that
    season) among the team's players who played, in proportion to his minutes."""
    played = rows[(rows["minutes"] > 0) & rows["pen_order"].notna()]
    first = played.sort_values("pen_order").drop_duplicates(["season", "fixture", "team_code"])
    charge = pd.Series(0.0, index=rows.index)
    charge[first.index] = PENALTIES_PER_TEAM_MATCH * PENALTY_XG * first["minutes"].clip(upper=90) / 90
    return rows["expected_goals"] - charge


def player_rates(history, players, as_of, p):
    """Per-90 rates per player code: observed (time-decayed) plus a price-based prior."""
    rows = history[history["kickoff_time"] < as_of].copy()
    rows["np_xg"] = non_penalty_xg(rows) if "pen_order" in rows else rows["expected_goals"]
    w = decay(rows["kickoff_time"], as_of, p.half_life_days)
    priors = _price_priors(rows)
    out = players[["code", "position", "price"]].drop_duplicates("code").set_index("code")
    k = p.prior_minutes
    for stat in RATES:
        recorded = rows[stat].notna()          # minutes only count where the stat was recorded
        num = (rows.loc[recorded, stat] * w[recorded]).groupby(rows.loc[recorded, "code"]).sum()
        mins = (rows.loc[recorded, "minutes"] * w[recorded]).groupby(rows.loc[recorded, "code"]).sum()
        num, mins = num.reindex(out.index, fill_value=0), mins.reindex(out.index, fill_value=0)
        fit = out["position"].map(lambda pos: priors.get((pos, stat), (0.0, 0.0)))
        prior = (fit.str[0] + fit.str[1] * out["price"]).clip(lower=0)
        out[stat] = (num + prior * k / 90) / (mins / 90 + k / 90)
    # FPL awards more assists than xA predicts, and goals track xG closely: calibrate by position.
    pos_rows = rows.assign(w=w)
    cal = {}
    for pos, g in pos_rows.groupby("position"):
        cal[(pos, "goal")] = ((g.goals_scored * g.w).sum() + 30) / ((g.expected_goals * g.w).sum() + 30)
        cal[(pos, "assist")] = ((g.assists * g.w).sum() + 30) / ((g.expected_assists * g.w).sum() + 30)
    out["goal_cal"] = out["position"].map(lambda pos: cal.get((pos, "goal"), 1.0))
    out["assist_cal"] = out["position"].map(lambda pos: cal.get((pos, "assist"), 1.0))
    return out


# ---------------------------------------------------------------- minutes

def minutes_model(history, as_of, p):
    """Per player code: P(start), P(60+ | start), minutes when starting / when 60+ / off the bench."""
    rows = history[history["kickoff_time"] < as_of].sort_values("kickoff_time")
    recency = rows.groupby("code").cumcount(ascending=False)
    w = 0.5 ** (recency / p.minutes_half_life)
    start = rows["starts"] > 0
    sixty = start & (rows["minutes"] >= 60)
    sub = ~start & (rows["minutes"] > 0)
    g = pd.DataFrame({
        "W": w, "S": w * start, "Q": w * sixty, "U": w * sub,
        "MS": w * start * rows["minutes"], "M60": w * sixty * rows["minutes"],
        "MU": w * sub * rows["minutes"],
    }).groupby(rows["code"]).sum()
    return pd.DataFrame({
        "p_start": (g.S + 0.3 * 0.5) / (g.W + 0.5),
        "p_60": (g.Q + 0.85) / (g.S + 1),
        "min_start": (g.MS + 80) / (g.S + 1),
        "min_60": (g.M60 + 87) / (g.Q + 1),
        "p_sub": (g.U + 0.1 * 0.5) / (g.W + 0.5),
        "min_sub": (g.MU + 20) / (g.U + 1),
    })


LIVE_START_CEILING = {"GK": 0.96, "DEF": 0.92, "MID": 0.92, "FWD": 0.92}


def max_start(p):
    """The highest P(start) the minutes model can give: a player who started every match."""
    weight = 1 / (1 - 0.5 ** (1 / p.minutes_half_life))
    return (weight + 0.3 * 0.5) / (weight + 0.5)


NO_MINUTES = {"p_start": 0.02, "p_60": 0.8, "min_start": 75, "min_60": 85, "p_sub": 0.05, "min_sub": 15}
RETURN_DATE = re.compile(r"(?:expected back|until|return(?:s|ing)? on|available from)\s+(\d{1,2} [A-Za-z]{3})", re.I)


def return_date(news, as_of):
    found = RETURN_DATE.search(news or "")
    if not found:
        return None
    for year in (as_of.year, as_of.year + 1, as_of.year - 1):
        try:
            date = pd.Timestamp(f"{found.group(1)} {year}", tz="UTC")
        except ValueError:
            return None
        if as_of - pd.Timedelta(days=60) <= date <= as_of + pd.Timedelta(days=300):
            return date
    return None


def availability(status, chance, news, kickoff, gw_offset, as_of):
    """Chance the player is available for a fixture, from FPL's status, chance of playing and news.
    gw_offset is 0 for the next gameweek, 1 for the one after, and so on."""
    if status == "a":
        return 1.0
    if status == "u":                                    # left the club or on loan elsewhere
        return 0.0
    back = return_date(news, as_of)
    if back is not None:
        return 0.0 if kickoff < back else 1.0
    c = (chance if chance == chance else 0.0) / 100   # NaN-safe
    recovery = {"d": 0.35, "s": 0.3, "n": 0.2}.get(status, 0.7)   # 'i' (injured) recovers slowest
    return 1 - (1 - c) * recovery ** gw_offset


# ---------------------------------------------------------------- projection

def penalty_share(pf):
    """Chance each player takes his team's penalties in a fixture: he's on the pitch and everyone
    above him on FPL's list isn't (a stand-in inherits them when the regular taker is out)."""
    share = pd.Series(0.0, index=pf.index)
    if "pen_order" not in pf:
        return share
    on_pitch = (pf["emin"] / 90).clip(0, 1)
    listed = pf[pf["pen_order"].notna()].sort_values("pen_order")
    nobody_above = (1 - on_pitch[listed.index]).groupby([listed["fixture"], listed["team"]]).transform(
        lambda s: s.cumprod().shift(fill_value=1.0))
    share[listed.index] = on_pitch[listed.index] * nobody_above
    return share


def _expected_floor_div(lam, step):
    """E[floor(X / step)] for X ~ Poisson(lam), vectorised."""
    k = np.arange(0, 40)
    pmf = poisson.pmf(k[None, :], np.asarray(lam)[:, None])
    return (pmf * (k // step)[None, :]).sum(axis=1)


def project(history, players, fixtures, teams, gws, as_of, p=Params(), start_news=None):
    """Expected points for every player in `players` for each gameweek in `gws`.

    players:  id, code, position, team (this season's team id), price, status, chance, news
    fixtures: id, event, team_h, team_a, kickoff_time
    start_news: {player id: chance to start} for the first gameweek, from the Claude news check. It
    replaces the model's chance to start (and FPL's "doubtful" percentage) unless FPL rules him out.
    Returns (per_gw, per_fixture): per_gw has one row per player with a column per gameweek.
    """
    code_of = teams.set_index("id")["code"]
    ratings = team_ratings(history, as_of, list(code_of), p)
    rates = player_rates(history, players, as_of, p)
    mins = minutes_model(history, as_of, p)

    fx = fixtures[fixtures["event"].isin(gws)]
    sides = pd.concat([
        pd.DataFrame({"fixture": fx.id, "gw": fx.event, "kickoff": fx.kickoff_time, "team": fx.team_h,
                      "opp": fx.team_a, "home": True}),
        pd.DataFrame({"fixture": fx.id, "gw": fx.event, "kickoff": fx.kickoff_time, "team": fx.team_a,
                      "opp": fx.team_h, "home": False}),
    ])
    cols = ["id", "code", "web_name", "position", "team", "status", "chance", "news"]
    pf = players[cols + [c for c in ["pen_order"] if c in players]].merge(sides, on="team")
    if pf.empty:
        return pd.DataFrame(index=players["id"]), pf
    pf["team_code"], pf["opp_code"] = pf["team"].map(code_of), pf["opp"].map(code_of)
    first_gw = min(gws)
    pf["avail"] = [availability(s, c, n, k, g - first_gw, as_of)
                   for s, c, n, k, g in zip(pf.status, pf.chance, pf.news, pf.kickoff, pf.gw)]
    pf = pf.join(rates.drop(columns=["position", "price"]), on="code")
    pf = pf.join(mins, on="code")
    for col, default in NO_MINUTES.items():
        pf[col] = pf[col].fillna(default)
    if p.start_ceiling:
        pf["p_start"] *= pf["position"].map(p.start_ceiling) / max_start(p)
    if start_news:
        news = (pf["gw"] == first_gw) & pf["id"].isin(start_news) & (pf["avail"] > 0)
        pf.loc[news, "p_start"] = pf.loc[news, "id"].map(start_news)
        pf.loc[news, "avail"] = 1.0

    mu, a, d = ratings["mu"], ratings["attack"], ratings["defence"]
    hf = np.where(pf["home"], ratings["home"], 1 / ratings["home"])
    a_t, d_t = pf.team_code.map(a).fillna(1), pf.team_code.map(d).fillna(1)
    a_o, d_o = pf.opp_code.map(a).fillna(1), pf.opp_code.map(d).fillna(1)
    pf["lam_for"] = mu * a_t * d_o * hf
    pf["lam_against"] = mu * a_o * d_t / hf
    att = d_o * hf                         # how much easier than average this fixture is to score in
    A, s, q = pf["avail"], pf["p_start"], pf["p_60"]
    emin = A * (s * pf.min_start + pf.p_sub * pf.min_sub)
    pos = pf["position"]

    pf["appearance"] = A * (2 * s * q + s * (1 - q) + pf.p_sub)
    pf["emin"] = emin
    pf["p_taker"] = penalty_share(pf)
    pf["pen_attempts"] = PENALTIES_PER_TEAM_MATCH * pf.lam_for / mu * pf.p_taker
    pf["xg"] = pf.np_xg / 90 * emin * att * pf.goal_cal + pf.pen_attempts * PENALTY_XG
    pf["xa"] = pf.expected_assists / 90 * emin * att * pf.assist_cal
    pf["goals"] = pf.xg * pos.map(GOAL_PTS)
    pf["assists"] = 3 * pf.xa
    pf["p_cs"] = A * s * q * np.exp(-pf.lam_against * pf.min_60 / 90)
    pf["clean_sheet"] = pf.p_cs * pos.map(CS_PTS)
    gk_def = pos.isin(["GK", "DEF"])
    pf["conceded"] = -np.where(gk_def, A * s * _expected_floor_div(pf.lam_against * pf.min_start / 90, 2), 0)
    save_lam = pf.saves / 90 * pf.min_start * (a_o / hf)
    pf["save_pts"] = np.where(pos == "GK", A * s * _expected_floor_div(save_lam, 3), 0)
    dc_mean = (pf.defensive_contribution / 90 * pf.min_start).clip(lower=1e-6)
    size = p.defcon_dispersion
    p_dc = nbinom.sf(pos.map(DEFCON_AT) - 1, size, size / (size + dc_mean))
    pf["defcon"] = 2 * A * s * np.where(pos == "GK", 0, p_dc)

    # Bonus follows the player's BPS habits, scaled by how much better than neutral this fixture is.
    neutral = (pf.np_xg / 90 * emin * pf.goal_cal * pos.map(GOAL_PTS)
               + 3 * pf.expected_assists / 90 * emin * pf.assist_cal
               + A * s * q * np.exp(-mu * d_t * pf.min_60 / 90) * pos.map(CS_PTS))
    fixture_core = pf.goals + pf.assists + pf.clean_sheet
    boost = (fixture_core / neutral.where(neutral > 0)).fillna(1).clip(0.5, 2.0)
    pf["bonus_pts"] = pf.bonus / 90 * emin * boost
    pf["cards"] = -pf.yellow_cards / 90 * emin
    pf["pen_miss"] = -2 * pf.pen_attempts * (1 - PENALTY_XG)
    parts = ["appearance", "goals", "assists", "clean_sheet", "conceded", "save_pts", "defcon", "bonus_pts",
             "cards", "pen_miss"]
    pf["xp"] = pf[parts].sum(axis=1)

    per_gw = pf.pivot_table(index="id", columns="gw", values="xp", aggfunc="sum").reindex(
        index=players["id"], columns=gws).fillna(0.0)
    return per_gw, pf
