"""Read-only recommendations for your FPL team: transfers, XI, captain, chips. Changes nothing.

python3 recommend.py --team 1234567              print the report (public data, no login)
python3 recommend.py --team 1234567 --email      print it and email it
options: --horizon 6 (gameweeks to plan over), --ft N (override free transfers)
"""
import argparse
from dataclasses import dataclass, replace

import pandas as pd

import chips as chip_planner
import data
import executor
import model
import optimizer
import team as team_module

HORIZON = 6


@dataclass
class Analysis:
    base: optimizer.Plan          # the plan to act on this week
    wait: optimizer.Plan          # the same, with no transfers this week
    gain: float                   # base vs. wait, expected points over the horizon
    held_back: int                # moves in the full plan that didn't clear the per-transfer bar
    chips: list                   # chips available this half
    expires: int                  # last gameweek of this half
    chip_table: pd.DataFrame
    chip_plan: dict               # chip -> gameweek, the best spread
    chip_proposal: str | None     # chip to ask about this week
    wildcard_gain: float


def plan_this_week(per_gw, players, team, gw, settings=optimizer.Settings()):
    """The plan to act on, the plan that waits a week, how much better acting is, and how many of
    the full plan's moves were held back.

    Every transfer made now has to earn executor.GAIN_PER_TRANSFER on its own: the best plan with
    k transfers this week must beat the best plan with k-1 by that much. If the last one doesn't,
    the plan with one fewer is tried, so a weak move can't ride along with a strong one."""
    args = (per_gw, players, team.squad, team.bank, team.free_transfers, team.sell_prices)
    wait = optimizer.plan(*args, replace(settings, frozen_gws={gw}))
    if not wait.weeks:
        raise RuntimeError(f"Optimizer found no plan: {wait.status}")
    if not team.transfers_open:              # a Wildcard or Free Hit is active: transfers are yours
        return wait, wait, 0.0, 0
    best = optimizer.plan(*args, settings)
    wanted = len(best.weeks[0].transfers_in)
    while best.weeks[0].transfers_in:
        count = len(best.weeks[0].transfers_in)
        fewer = wait if count == 1 else optimizer.plan(*args, replace(settings, transfer_limit={gw: count - 1}))
        if best.objective - fewer.objective >= executor.GAIN_PER_TRANSFER:
            break
        best = fewer
    return best, wait, best.objective - wait.objective, wanted - len(best.weeks[0].transfers_in)


def analyse(per_gw, players, team, horizon_gws):
    """per_gw: projections up to the chips' expiry (at least the horizon). The plan uses the horizon."""
    gw = horizon_gws[0]
    plan_xp = per_gw[horizon_gws]
    base, wait, gain, held = plan_this_week(plan_xp, players, team, gw)
    chips, expires = team.chips_available(gw)
    usable = [c for c in chips if c != "wildcard"]
    table = chip_planner.values(per_gw, players, team, base, usable, expires) if usable else pd.DataFrame()
    wild = chip_planner.wildcard_gain(plan_xp, players, team, base, gw) if "wildcard" in chips else float("nan")
    proposal, spread = chip_planner.propose(table, chips, gw, expires, wild)
    return Analysis(base, wait, gain, held, chips, expires, table, spread, proposal, wild)


def fixture_counts(fixtures, teams, gws):
    """Teams with a double or blank gameweek in the horizon."""
    notes = []
    short = teams.set_index("id")["short_name"]
    for gw in gws:
        fx = fixtures[fixtures.event == gw]
        count = pd.concat([fx.team_h, fx.team_a]).value_counts().reindex(teams.id, fill_value=0)
        doubles = [short[t] for t in count[count >= 2].index]
        blanks = [short[t] for t in count[count == 0].index]
        if doubles or blanks:
            notes.append(f"GW{gw}: " + "; ".join(
                part for part in [f"double: {', '.join(doubles)}" if doubles else "",
                                  f"blank: {', '.join(blanks)}" if blanks else ""] if part))
    return notes


def report(analysis, per_gw, pf, players, fixtures, teams, team, horizon_gws):
    by_id = players.set_index("id")
    name = lambda i: f"{by_id.loc[i, 'web_name']} ({by_id.loc[i, 'position']}, £{by_id.loc[i, 'price']:.1f})"
    base, first = analysis.base, analysis.base.weeks[0]
    lines = [f"{team.name}: GW{first.gw} recommendations", ""]
    lines.append(f"Bank £{team.bank:.1f}m, {team.free_transfers} free transfer(s). "
                 f"Chips left this half (expire after GW{analysis.expires}): "
                 + (", ".join(team_module.CHIPS[c] for c in analysis.chips) or "none"))
    lines.append("")
    if first.transfers_in:
        lines.append(f"TRANSFERS for GW{first.gw}" + (f" ({first.hits} hit, -{4 * first.hits})" if first.hits else ""))
        for out, inn in zip(first.transfers_out, first.transfers_in):
            lines.append(f"  OUT {name(out)}  ->  IN {name(inn)}   "
                         f"next {len(horizon_gws)} GWs: {per_gw.loc[out, horizon_gws].sum():.1f} -> "
                         f"{per_gw.loc[inn, horizon_gws].sum():.1f} xP")
        lines.append(f"  Making these moves now vs. waiting a week: {analysis.gain:+.1f} xP over the horizon "
                     f"(each move adds at least {executor.GAIN_PER_TRANSFER:.0f})")
    else:
        lines.append(f"TRANSFERS for GW{first.gw}: none. Banking the free transfer.")
    if analysis.held_back:
        lines.append(f"  {analysis.held_back} more move(s) added less than {executor.GAIN_PER_TRANSFER:.0f} xP each "
                     "over waiting, so they wait.")
    lines.append("")
    lines.append(f"STARTING XI for GW{first.gw} (expected {first.xp:.1f} with captain)")
    short = teams.set_index("id")["short_name"]
    for i in first.xi:
        tag = " (C)" if i == first.captain else " (VC)" if i == first.vice_captain else ""
        fx = pf[(pf.id == i) & (pf.gw == first.gw)]
        opp = ", ".join(f"{short[o]}{'(H)' if h else '(A)'}" for o, h in zip(fx.opp, fx.home)) or "no fixture"
        flag = f"  [{by_id.loc[i, 'status']}: {by_id.loc[i, 'news']}]" if by_id.loc[i, "status"] != "a" else ""
        lines.append(f"  {name(i):<34} {opp:<14} {per_gw.loc[i, first.gw]:>4.1f}{tag}{flag}")
    lines.append("BENCH (in order): " + ", ".join(f"{by_id.loc[i, 'web_name']} {per_gw.loc[i, first.gw]:.1f}"
                                                  for i in first.bench))
    lines.append("")
    lines.append("PLAN for the following weeks (re-planned every run)")
    for week in base.weeks[1:]:
        moves = ", ".join(f"{by_id.loc[o, 'web_name']} -> {by_id.loc[i, 'web_name']}"
                          for o, i in zip(week.transfers_out, week.transfers_in)) or "no transfers"
        lines.append(f"  GW{week.gw}: {moves}{f' ({week.hits} hit)' if week.hits else ''}; "
                     f"captain {by_id.loc[week.captain, 'web_name']}; expected {week.xp:.1f}")
    lines.append("")
    if analysis.chips:
        lines.append(chip_planner.describe(analysis.chip_table, analysis.chip_plan, analysis.chip_proposal,
                                           analysis.wildcard_gain))
    notes = fixture_counts(fixtures, teams, horizon_gws)
    if notes:
        lines.append("Doubles/blanks: " + " | ".join(notes))
    lines.append("")
    lines.append(top_projections(per_gw[horizon_gws], players))
    return "\n".join(lines)


def top_projections(per_gw, players, count=6):
    by_id = players.set_index("id")
    total = per_gw.sum(axis=1)
    lines = [f"TOP PROJECTIONS by position, GW{per_gw.columns[0]}-{per_gw.columns[-1]}"]
    for pos in ["GK", "DEF", "MID", "FWD"]:
        best = total.reindex(players[players.position == pos].id).nlargest(count)
        lines.append(f"  {pos}: " + ", ".join(f"{by_id.loc[i, 'web_name']} £{by_id.loc[i, 'price']:.1f} {v:.1f}"
                                              for i, v in best.items()))
    return "\n".join(lines)


def gameweeks(next_gw, team=None, horizon=HORIZON):
    """(horizon gameweeks, all gameweeks to project: the horizon, extended to the chips' expiry)."""
    horizon_gws = list(range(next_gw, min(next_gw + horizon, 39)))
    last = horizon_gws[-1]
    if team is not None:
        chips, expires = team.chips_available(next_gw)
        if chips:
            last = max(last, expires)
    return horizon_gws, list(range(next_gw, last + 1))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--team", type=int, help="your FPL team id (in the Points page URL); "
                        "without it, only the projections are printed")
    parser.add_argument("--horizon", type=int, default=HORIZON)
    parser.add_argument("--ft", type=int, help="override the free-transfer count")
    parser.add_argument("--email", action="store_true")
    args = parser.parse_args()

    season = data.load_live()
    history = data.load_with_prior(season)
    now = pd.Timestamp.now(tz="UTC")
    upcoming = season.events[season.events.deadline_time > now]
    if upcoming.empty:
        print("Season over.")
        return
    next_gw = int(upcoming.id.min())
    my_team = team_module.load(args.team, season.players) if args.team else None
    if my_team is not None and args.ft is not None:
        my_team = replace(my_team, free_transfers=args.ft)
    horizon_gws, all_gws = gameweeks(next_gw, my_team, args.horizon)
    per_gw, pf = model.project(history, season.players, season.fixtures, season.teams, all_gws, now,
                               model.Params(start_ceiling=model.LIVE_START_CEILING))
    print(f"Loaded {len(season.players)} players, {len(season.history)} match rows; projecting GW{all_gws[0]}-{all_gws[-1]}")
    if my_team is None:
        print(top_projections(per_gw[horizon_gws], season.players, count=10))
        return
    analysis = analyse(per_gw, season.players, my_team, horizon_gws)
    text = report(analysis, per_gw, pf, season.players, season.fixtures, season.teams, my_team, horizon_gws)
    print(text)
    if args.email:
        import mailer
        deadline = upcoming.deadline_time.min().tz_convert("America/New_York").strftime("%a %d %b %H:%M")
        mailer.send(f"GW{next_gw} plan (deadline {deadline})", text)


if __name__ == "__main__":
    main()
