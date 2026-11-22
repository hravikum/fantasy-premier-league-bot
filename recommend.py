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
import doc
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
    bar = executor.GAIN_PER_TRANSFER
    while best.weeks[0].transfers_in:
        count = len(best.weeks[0].transfers_in)
        fewer = wait if count == 1 else optimizer.plan(*args, replace(settings, transfer_limit={gw: count - 1}))
        # the last move earns its keep, and the whole set beats waiting by the bar for every move
        if best.objective - fewer.objective >= bar and best.objective - wait.objective >= bar * count:
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
    """The weekly report as email blocks (doc.py)."""
    by_id = players.set_index("id")
    base, first = analysis.base, analysis.base.weeks[0]
    span = f"next {len(horizon_gws)} GWs"
    chips_left = ", ".join(team_module.CHIPS[c] for c in analysis.chips) or "none"
    blocks = [doc.Heading(f"Transfers for GW{first.gw}" + (f" ({first.hits} hit, -{4 * first.hits})" if first.hits else ""))]
    if first.transfers_in:
        rows = [[f"{by_id.loc[o, 'web_name']} £{by_id.loc[o, 'price']:.1f}", "→",
                 f"{by_id.loc[i, 'web_name']} £{by_id.loc[i, 'price']:.1f}", by_id.loc[i, "position"],
                 f"{per_gw.loc[o, horizon_gws].sum():.1f} → {per_gw.loc[i, horizon_gws].sum():.1f}"]
                for o, i in zip(first.transfers_out, first.transfers_in)]
        blocks.append(doc.Table(["Out", "", "In", "Pos", f"xP, {span}"], rows, right={4}))
        blocks.append(doc.Para([doc.Tone(f"{analysis.gain:+.1f} expected points", "good", bold=True),
                                f" over the horizon vs. waiting a week (each move adds at least "
                                f"{executor.GAIN_PER_TRANSFER:.0f})."]))
    else:
        blocks.append(doc.Para("None this week: banking the free transfer.", tone="muted"))
    if analysis.held_back:
        blocks.append(doc.Para(f"{analysis.held_back} more move(s) in the full plan added less than "
                               f"{executor.GAIN_PER_TRANSFER:.0f} expected point each over waiting, so they wait.",
                               tone="muted"))

    blocks.append(doc.Heading(f"Starting XI for GW{first.gw}: {first.xp:.1f} expected with captain"))
    blocks += lineup_blocks(first, per_gw, pf, players, teams)

    blocks.append(doc.Heading("Plan for the following weeks"))
    rows = []
    for week in base.weeks[1:]:
        moves = ", ".join(f"{by_id.loc[o, 'web_name']} → {by_id.loc[i, 'web_name']}"
                          for o, i in zip(week.transfers_out, week.transfers_in)) or doc.Tone("no transfers", "muted")
        rows.append([f"GW{week.gw}", [moves, doc.Badge(f"{week.hits} hit", "warn")] if week.hits else moves,
                     by_id.loc[week.captain, "web_name"], f"{week.xp:.1f}"])
    blocks.append(doc.Table(["", "Transfers", "Captain", "xP"], rows, right={3},
                            note="Re-planned every run, so later weeks can change."))
    if analysis.chips:
        blocks += chip_planner.describe(analysis.chip_table, analysis.chip_plan, analysis.chip_proposal,
                                        analysis.wildcard_gain,
                                        left=f"Left this half: {chips_left} (they expire after GW{analysis.expires}).")
    notes = fixture_counts(fixtures, teams, horizon_gws)
    if notes:
        blocks.append(doc.Para("Doubles and blanks: " + " | ".join(notes)))
    blocks += top_projections(per_gw[horizon_gws], players)
    return blocks


def lineup_blocks(week, per_gw, pf, players, teams, chip=None):
    """The XI (keeper first, then by position) with opponents and captain badges, then the bench."""
    by_id = players.set_index("id")
    short = teams.set_index("id")["short_name"]
    order = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
    rows, tones = [], {}
    for n, i in enumerate(sorted(week.xi, key=lambda i: (order[by_id.loc[i, "position"]], -per_gw.loc[i, week.gw]))):
        fx = pf[(pf.id == i) & (pf.gw == week.gw)]
        opp = ", ".join(f"{short[o]} ({'H' if h else 'A'})" for o, h in zip(fx.opp, fx.home)) or "no match"
        badge = [doc.Badge("TC" if chip == "3xc" else "C")] if i == week.captain else \
            [doc.Badge("VC", "muted")] if i == week.vice_captain else []
        name = [by_id.loc[i, "web_name"], *badge]
        if by_id.loc[i, "status"] != "a":
            name += [" ", doc.Tone(by_id.loc[i, "news"] or by_id.loc[i, "status"], "warn")]
            tones[n] = "warn"
        rows.append([name, by_id.loc[i, "position"], opp, f"{per_gw.loc[i, week.gw]:.1f}"])
    bench = " · ".join(f"{by_id.loc[i, 'web_name']} {per_gw.loc[i, week.gw]:.1f}" for i in week.bench)
    return [doc.Table(["Player", "Pos", "Opponent", "xP"], rows, right={3}, row_tones=tones),
            doc.Para([doc.Tone("Bench" + (" (Bench Boost: they count)" if chip == "bboost" else "") + ": ", bold=True),
                      bench])]


def top_projections(per_gw, players, count=6):
    by_id = players.set_index("id")
    total = per_gw.sum(axis=1)
    rows = []
    for pos in ["GK", "DEF", "MID", "FWD"]:
        best = total.reindex(players[players.position == pos].id).nlargest(count)
        rows.append([pos, " · ".join(f"{by_id.loc[i, 'web_name']} £{by_id.loc[i, 'price']:.1f} ({v:.1f})"
                                    for i, v in best.items())])
    return [doc.Heading(f"Top projections, GW{per_gw.columns[0]}-{per_gw.columns[-1]}"),
            doc.Table(["", "Expected points over the horizon"], rows)]


def money(team):
    return f"£{team.bank:.1f}m in the bank · {team.free_transfers} free transfer{'s' if team.free_transfers != 1 else ''}"


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
        print(doc.to_text(doc.Message("Projections", blocks=top_projections(per_gw[horizon_gws], season.players, 10))))
        return
    analysis = analyse(per_gw, season.players, my_team, horizon_gws)
    deadline = upcoming.deadline_time.min().tz_convert("America/New_York").strftime("%a %d %b %H:%M")
    message = doc.Message(f"GW{next_gw} plan", f"Deadline {deadline} ET · {my_team.name} · {money(my_team)} · public data",
                          blocks=report(analysis, per_gw, pf, season.players, season.fixtures, season.teams,
                                        my_team, horizon_gws))
    print(doc.to_text(message))
    if args.email:
        import mailer
        mailer.send(f"GW{next_gw} plan (deadline {deadline})", doc.to_text(message), doc.to_html(message))


if __name__ == "__main__":
    main()
