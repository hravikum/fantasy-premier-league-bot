"""Why the model rates a player the way it does.

python3 explain.py Haaland              projection breakdown for the next 6 gameweeks
python3 explain.py "B.Fernandes" --horizon 4
python3 explain.py Haaland --team 1234567     also: what he's worth to your team (the best plan
                                              with him vs. the best plan without him)

Shows his actual output this season and last, what the model takes from it (chance to start,
per-90 rates), his team's attack/defence ratings (with and without last season's matches), each
fixture's expected points by scoring category, and how he compares on points per £m.
"""
import argparse

import pandas as pd

import data
import model
import optimizer
import team as team_module

PARTS = ["appearance", "goals", "assists", "clean_sheet", "conceded", "save_pts", "defcon", "bonus_pts", "cards",
         "pen_miss"]


def season_line(rows, label):
    if rows.empty or rows.minutes.sum() == 0:
        return f"  {label}: no minutes"
    m = rows.minutes.sum()
    per90 = lambda col: rows[col].sum() / m * 90
    return (f"  {label}: {int((rows.minutes > 0).sum())} apps ({int(rows.starts.sum())} starts), {int(m)} min, "
            f"{int(rows.goals_scored.sum())} G ({rows.expected_goals.sum():.1f} xG), "
            f"{int(rows.assists.sum())} A ({rows.expected_assists.sum():.1f} xA), {int(rows.total_points.sum())} pts | "
            f"per 90: xG {per90('expected_goals'):.2f}, xA {per90('expected_assists'):.2f}, "
            f"pts {rows.total_points.sum() / max(len(rows), 1):.1f} per team match")


def team_lines(history, season, team_code, as_of):
    """The team's results and xG this season vs last, and its ratings with and without last season."""
    lines = []
    for name in [data.previous_season(season), season]:
        rows = history[(history.season == name) & (history.team_code == team_code)]
        if rows.empty:
            continue
        m = rows.groupby("fixture").agg(xg=("expected_goals", "sum"), xgc=("expected_goals_conceded", "max"),
                                        gf=("goals_scored", "sum"), ga=("goals_conceded", "max"))
        lines.append(f"  {name}: {len(m)} matches, scored {m.gf.mean():.2f}/match (xG {m.xg.mean():.2f}), "
                     f"conceded {m.ga.mean():.2f}/match (xGA {m.xgc.mean():.2f})")
    for label, rows in [("model (this season + last, fading)", history),
                        ("this season only", history[history.season == season])]:
        r = model.team_ratings(rows, as_of, [team_code], model.Params())
        lines.append(f"  ratings, {label}: attack {r['attack'][team_code]:.2f}, defence {r['defence'][team_code]:.2f} "
                     "(1.00 = league average; defence below 1 is better)")
    return lines


def worth_to_team(team_id, pid, per_gw, players, ft=None):
    """For a player you don't own: the best plan for your squad with him available vs. never buying him."""
    mine = team_module.load(team_id, players)
    if ft is not None:
        mine.free_transfers = ft
    if pid in mine.squad:
        return ["(He's already in your squad.)"]
    args = (per_gw, players, mine.squad, mine.bank, mine.free_transfers, mine.sell_prices)
    best = optimizer.plan(*args)
    without = optimizer.plan(*args, optimizer.Settings(banned={pid}))
    name = players.set_index("id")["web_name"]
    moves = lambda plan: "; ".join(
        f"GW{w.gw}: " + ", ".join(f"{name[o]} -> {name[i]}" for o, i in zip(w.transfers_out, w.transfers_in))
        for w in plan.weeks if w.transfers_in) or "no transfers"
    return [f"WHAT HE'S WORTH TO {mine.name.upper()} (bank £{mine.bank:.1f}m, {mine.free_transfers} free transfers)",
            f"  best plan: {best.objective:.1f} expected points over {per_gw.shape[1]} GWs (discounted, after hits)",
            f"    {moves(best)}",
            f"  never buying him: {without.objective:.1f}  ->  he adds {best.objective - without.objective:+.1f}",
            f"    {moves(without)}"]


def explain(name, horizon=6, team_id=None, ft=None):
    season = data.load_live()
    history = data.load_with_prior(season)
    now = pd.Timestamp.now(tz="UTC")
    gw = int(season.events[season.events.deadline_time > now].id.min())
    gws = list(range(gw, min(gw + horizon, 39)))
    players = season.players
    match = players[players.web_name.str.lower() == name.lower()]
    if match.empty:
        match = players[players.web_name.str.lower().str.contains(name.lower(), regex=False)]
    if match.empty:
        raise SystemExit(f"No player called {name!r}.")
    p = match.sort_values("selected_by", ascending=False).iloc[0]
    per_gw, pf = model.project(history, players, season.fixtures, season.teams, gws, now,
                               model.Params(start_ceiling=model.LIVE_START_CEILING))
    short = season.teams.set_index("id")["short_name"]
    rows = history[history.code == p.code]
    team_code = season.teams.set_index("id").loc[p.team, "code"]

    out = [f"{p.web_name} ({p.position}, {short[p.team]}, £{p.price:.1f}, {p.selected_by:.1f}% owned), "
           f"FPL status {p.status}{': ' + p.news if p.news else ''}"
           + (f", penalties order {int(p.pen_order)}" if p.pen_order == p.pen_order else ""), "",
           "WHAT HE'S DONE"]
    out += [season_line(rows[rows.season == data.previous_season(season.name)], "last season"),
            season_line(rows[rows.season == season.name], "this season")]
    recent = rows[rows.season == season.name].tail(6)
    out.append("  last 6: " + ", ".join(f"GW{int(r.gw)} {int(r.minutes)}' {r.expected_goals:.2f}xG "
                                        f"{int(r.goals_scored)}G {int(r.total_points)}pts" for r in recent.itertuples()))
    one = pf[pf.id == p.id].iloc[0]
    out += ["", "WHAT THE MODEL TAKES FROM IT",
            f"  chance to start {one.p_start:.0%}, minutes if he starts {one.min_start:.0f}, "
            f"non-penalty xG per 90 {one.np_xg:.2f}, xA per 90 {one.expected_assists:.2f}, bonus per 90 {one.bonus:.2f}",
            f"  (rates blend this season and last, older matches fading by half every year, plus a price-based "
            f"prior worth {model.Params().prior_minutes:.0f} minutes)", "", f"{short[p.team]} AS A TEAM"]
    out += team_lines(history, season.name, team_code, now)

    out += ["", "EACH FIXTURE (expected points by category)"]
    mine = pf[pf.id == p.id].sort_values(["gw", "kickoff"])
    header = f"  {'GW':<4}{'opp':<8}{'team xG':>8}{'his xG':>7}{'xA':>6}" + "".join(f"{c[:6]:>8}" for c in PARTS) + f"{'total':>8}"
    out.append(header)
    for r in mine.itertuples():
        out.append(f"  {r.gw:<4}{short[r.opp] + ('(H)' if r.home else '(A)'):<8}{r.lam_for:>8.2f}{r.xg:>7.2f}{r.xa:>6.2f}"
                   + "".join(f"{getattr(r, c):>8.2f}" for c in PARTS) + f"{r.xp:>8.2f}")
    total = per_gw.loc[p.id].sum()
    out.append(f"  next {len(gws)} GWs: {total:.1f} expected points ({total / len(gws):.2f} a week); "
               f"as captain each week: +{total:.1f} more")

    same = players[players.position == p.position].set_index("id")
    table = pd.DataFrame({"name": same.web_name, "price": same.price, "xp": per_gw.sum(axis=1).reindex(same.index)})
    table["per_m"] = table.xp / table.price
    top = table.nlargest(8, "xp")
    out += ["", f"OTHER {p.position}s, next {len(gws)} GWs (expected points, per £m)"]
    out += [f"  {r.name:<16} £{r.price:>4.1f} {r.xp:>6.1f} {r.per_m:>6.2f}" for r in top.itertuples()]
    if team_id:
        out += [""] + worth_to_team(team_id, p.id, per_gw, players, ft)
    return "\n".join(out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("player")
    parser.add_argument("--horizon", type=int, default=6)
    parser.add_argument("--team", type=int, help="your FPL team id: also show what he's worth to your team")
    parser.add_argument("--ft", type=int, help="override the free-transfer count (public data can be off by one)")
    args = parser.parse_args()
    print(explain(args.player, args.horizon, args.team, args.ft))
