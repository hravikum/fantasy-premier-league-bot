"""Why the model rates a player the way it does.

python3 explain.py Haaland              projection breakdown for the next 6 gameweeks
python3 explain.py "B.Fernandes" --horizon 4
python3 explain.py Haaland --team 1234567     also: what he's worth to your team (the best plan
                                              with him vs. the best plan without him)
python3 explain.py --squad --team 1234567     squad check: ownership, our projections vs FPL's and the
python3 explain.py --squad=3 --team 1234567   crowd's, club exposure, defences, and what capping a club
                                              costs (=3 overrides the free transfers)

Shows his actual output this season and last, what the model takes from it (chance to start,
per-90 rates), his team's attack/defence ratings (with and without last season's matches), each
fixture's expected points by scoring category, and how he compares on points per £m.
"""
import argparse

import numpy as np
import pandas as pd

import data
import model
import optimizer
import recommend
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


# ---------------------------------------------------------------- squad check

DEFENSIVE = ("GK", "DEF")


def _moves(plan, name, weeks=None):
    return "; ".join(f"GW{w.gw}: " + ", ".join(f"{name[o]} -> {name[i]}" for o, i in zip(w.transfers_out, w.transfers_in))
                     for w in plan.weeks[:weeks] if w.transfers_in) or "no transfers"


def _clubs(squad, players, short):
    by_id = players.set_index("id")
    groups = {}
    for i in squad:
        groups.setdefault(by_id.loc[i, "team"], []).append(i)
    return sorted(groups.items(), key=lambda kv: -len(kv[1])), by_id


def squad_check(team_id, ft=None, horizon=6):
    """How template the team is, how our projections compare with FPL's and with ownership, club
    exposure, the defences it leans on, and what capping a club would cost."""
    season = data.load_live()
    history = data.load_with_prior(season)
    now = pd.Timestamp.now(tz="UTC")
    gw = int(season.events[season.events.deadline_time > now].id.min())
    gws = list(range(gw, min(gw + horizon, 39)))
    players, teams = season.players, season.teams
    per_gw, pf = model.project(history, players, season.fixtures, teams, gws, now,
                               model.Params(start_ceiling=model.LIVE_START_CEILING))
    mine = team_module.load(team_id, players)
    if ft is not None:
        mine.free_transfers = ft
    by_id = players.set_index("id")
    name, short = by_id["web_name"], teams.set_index("id")["short_name"]
    week = recommend.plan_this_week(per_gw, players, mine, gw)
    first = week.base.weeks[0]
    after = [i for i in mine.squad if i not in first.transfers_out] + first.transfers_in
    total = per_gw.sum(axis=1)
    order = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
    out = [f"SQUAD CHECK: {mine.name}, GW{gws[0]}-{gws[-1]} (public data: squad as of the last deadline, "
           f"£{mine.bank:.1f}m bank, {mine.free_transfers} free transfers)"]
    try:
        rank = data.get(f"entry/{team_id}/").get("summary_overall_rank")
        if rank:
            out.append(f"Overall rank: {rank:,}")
    except Exception:
        pass
    out.append(f"This week's plan: {_moves(week.base, name, 1)}")

    # 1. ownership
    out += ["", "1. OWNERSHIP (% of all FPL managers who own him) and projections",
            f"  {'player':<16}{'club':<6}{'pos':<5}{'price':>6}{'own%':>7}{'ours GW' + str(gw):>10}{'FPL ep':>8}"
            f"{'ours ' + str(len(gws)) + 'GW':>9}  note"]
    rows = sorted(set(mine.squad) | set(first.transfers_in), key=lambda i: (order[by_id.loc[i, "position"]], -total[i]))
    for i in rows:
        r = by_id.loc[i]
        note = "IN" if i in first.transfers_in else "OUT" if i in first.transfers_out else ""
        out.append(f"  {r.web_name:<16}{short[r.team]:<6}{r.position:<5}{r.price:>6.1f}{r.selected_by:>7.1f}"
                   f"{per_gw.loc[i, gw]:>10.1f}{r.ep_next:>8.1f}{total[i]:>9.1f}  {note}")
    shared = lambda xi: by_id.loc[xi, "selected_by"].sum() / 100
    out.append(f"  An average manager owns {shared(week.wait.weeks[0].xi):.1f} of your current starting XI, "
               f"{shared(first.xi):.1f} of it after this week's moves.")
    popular = players[players.selected_by >= 20].sort_values("selected_by", ascending=False)
    out.append("  Owned by 20%+ of managers: " + ", ".join(
        f"{r.web_name} {r.selected_by:.0f}%{' (yours)' if r.id in after else ''}" for r in popular.itertuples()))

    # 2. our projections vs FPL's and the crowd's
    first_rows = pf[pf.gw == gw].groupby("id").apply(lambda g: (g.p_start * g.avail).max(), include_groups=False)
    regulars = first_rows[first_rows >= 0.5].index
    cmp = pd.DataFrame({"ours_gw": per_gw[gw], "ours": total, "fpl": by_id.ep_next, "own": by_id.selected_by,
                        "price": by_id.price}).reindex(regulars).dropna()
    rho = lambda a, b: cmp[a].corr(cmp[b], method="spearman")
    out += ["", f"2. HOW OUR PROJECTIONS COMPARE ({len(cmp)} likely starters; Spearman rank correlation, 1 = same order)",
            f"  ours (GW{gw}) vs FPL's ep_next: {rho('ours_gw', 'fpl'):.2f}",
            f"  ours ({len(gws)} GWs) vs ownership: {rho('ours', 'own'):.2f}    FPL's ep_next vs ownership: {rho('fpl', 'own'):.2f}",
            f"  ours vs price: {rho('ours', 'price'):.2f}    ownership vs price: {rho('own', 'price'):.2f}"]
    for pos in ["GK", "DEF", "MID", "FWD"]:
        sub = cmp[by_id.loc[cmp.index, "position"] == pos].copy()
        sub["value"] = sub.ours / sub.price
        sub["own_rank"] = sub.own.rank(ascending=False)
        out.append(f"  {pos}, our top 8 over {len(gws)} GWs: " + " · ".join(
            f"{name[i]} {short[by_id.loc[i, 'team']]} {r.ours:.1f} ({r.own:.0f}% own, #{r.own_rank:.0f} by own)"
            for i, r in sub.nlargest(8, "ours").iterrows()))

    # 3. club exposure
    groups, _ = _clubs(after, players, short)
    out += ["", "3. CLUB EXPOSURE after this week's moves"]
    for team, ids in groups:
        if len(ids) >= 2:
            out.append(f"  {short[team]}: " + ", ".join(f"{name[i]} {by_id.loc[i, 'position']}" for i in ids))
    final = week.base.weeks[-1]
    groups_end, _ = _clubs(final.xi + final.bench, players, short)
    out.append(f"  At the end of the plan (GW{final.gw}): " + ", ".join(
        f"{short[t]} {len(ids)}" for t, ids in groups_end if len(ids) >= 2))

    # 4. the defences the squad leans on
    codes = teams.set_index("id")["code"]
    current = teams.code.tolist()
    blended = model.team_ratings(history, now, current, model.Params())["defence"]
    this_season = model.team_ratings(history[history.season == season.name], now, current, model.Params())["defence"]
    rank_of = lambda ratings, code: int(pd.Series(ratings).reindex(current).rank().loc[code])
    out += ["", "4. DEFENCES YOU'RE BACKING (rank 1 = best of 20; defence rating below 1 = better than average)"]
    for team, ids in groups:
        backs = [i for i in ids if by_id.loc[i, "position"] in DEFENSIVE]
        if len(backs) < 2:
            continue
        code = codes[team]
        out.append(f"  {short[team]} ({', '.join(name[i] for i in backs)}): model rank {rank_of(blended, code)} "
                   f"(rating {blended[code]:.2f}); this season only rank {rank_of(this_season, code)} "
                   f"(rating {this_season[code]:.2f})")
        out += ["  " + line for line in team_lines(history, season.name, code, now)[:2]]
        fx = pf[(pf.team == team) & pf.gw.isin(gws[:3])].drop_duplicates(["gw", "opp"])
        out.append("    clean-sheet chance, next 3: " + ", ".join(
            f"GW{r.gw} {short[r.opp]}({'H' if r.home else 'A'}) {np.exp(-r.lam_against):.0%}" for r in fx.itertuples()))
        for i in backs:
            rows_i = pf[(pf.id == i) & pf.gw.isin(gws)]
            defence = rows_i.clean_sheet.sum() + rows_i.conceded.sum() + rows_i.save_pts.sum()
            out.append(f"    {name[i]:<14} {total[i]:.1f} over {len(gws)} GWs: clean sheets/conceded/saves {defence:.1f}, "
                       f"defensive contributions {rows_i.defcon.sum():.1f}, goals+assists "
                       f"{rows_i.goals.sum() + rows_i.assists.sum():.1f}, appearance/bonus/cards "
                       f"{rows_i.appearance.sum() + rows_i.bonus_pts.sum() + rows_i.cards.sum():.1f}")

    # 5. what limits would cost
    args = (per_gw, players, mine.squad, mine.bank, mine.free_transfers, mine.sell_prices)
    best = optimizer.plan(*args)
    heavy = [t for t, ids in groups if len(ids) >= 3]
    variants = [(f"at most 2 from {short[t]}", optimizer.Settings(club_max={t: 2})) for t in heavy]
    variants.append(("at most 2 keepers/defenders from any one club", optimizer.Settings(defence_max=2)))
    variants.append(("at most 1 keeper/defender from any one club", optimizer.Settings(defence_max=1)))
    out += ["", f"5. WHAT LIMITS WOULD COST (best plan over {len(gws)} GWs, discounted expected points; "
                f"unlimited: {best.objective:.1f})"]
    for label, settings in variants:
        capped = optimizer.plan(*args, settings)
        if not capped.weeks:
            out.append(f"  {label}: no plan ({capped.status})")
            continue
        out.append(f"  {label}: {capped.objective:.1f} ({capped.objective - best.objective:+.1f}); "
                   f"this week: {_moves(capped, name, 1)}")
    return "\n".join(out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("player", nargs="?")
    parser.add_argument("--squad", nargs="?", const=-1, type=int,
                        help="squad check instead of one player (--squad=N: N free transfers)")
    parser.add_argument("--horizon", type=int, default=6)
    parser.add_argument("--team", type=int, help="your FPL team id: also show what he's worth to your team")
    parser.add_argument("--ft", type=int, help="override the free-transfer count (public data can be off by one)")
    args = parser.parse_args()
    if args.squad is not None:
        if not args.team:
            parser.error("--squad needs --team")
        print(squad_check(args.team, args.ft if args.squad < 0 else args.squad, args.horizon))
    elif args.player:
        print(explain(args.player, args.horizon, args.team, args.ft))
    else:
        parser.error("name a player, or --squad")
