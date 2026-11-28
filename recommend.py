"""Read-only recommendations for your FPL team: transfers, XI, captain, chips. Changes nothing.

python3 recommend.py --team 1234567              print the report (public data, no login)
python3 recommend.py --team 1234567 --email      print it and email it
options: --horizon 6 (gameweeks to plan over), --ft N (override free transfers)
"""
import argparse
from collections import Counter
from dataclasses import dataclass, replace
from typing import NamedTuple

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
    gain: float                   # base vs. waiting (vs. only the rule-out sales, if any), over the horizon
    held_back: int                # moves in the full plan that didn't clear the per-transfer bar
    chips: list                   # chips available this half
    expires: int                  # last gameweek of this half
    chip_table: pd.DataFrame
    chip_plan: dict               # chip -> gameweek, the best spread
    chip_proposal: str | None     # chip to ask about this week
    chip_value: float             # what the proposed chip is worth this week
    sold: dict = None             # player id -> gameweeks he's ruled out: sold now under the rule-out rule
    kept: dict = None             # player id -> why a ruled-out player stays
    chip_pairs: dict = None       # (Wildcard week, Bench Boost week) -> value together
    unprepared: pd.DataFrame = None   # Bench Boost / Triple Captain values without setting up for them
    agreed: dict = None           # chip -> gameweek you agreed to plan around


class ThisWeek(NamedTuple):
    base: optimizer.Plan          # the plan to act on
    wait: optimizer.Plan          # no transfers this week
    gain: float                   # base vs. the plan making only the rule-out sales (vs. wait if there are none)
    held_back: int                # moves in the full plan that didn't clear the per-transfer bar
    sold: dict                    # player id -> "GW12–14": ruled out, so he goes now whatever the bar says
    kept: dict                    # player id -> why a ruled-out player stays this week


OUT_STATUSES = ("i", "s", "u", "n")      # injured, suspended, left the club, not eligible


def ruled_out(per_gw, players, squad, gw):
    """Squad players FPL rules out (0% chance) who project nothing this gameweek and the next one at
    least: {id: "GW12–14"}, the run of gameweeks he misses ("+" if it runs past the horizon)."""
    if "status" not in players.columns:
        return {}
    gws = [g for g in per_gw.columns if g >= gw]
    by_id = players.set_index("id")
    out = {}
    for i in squad:
        if i not in by_id.index or i not in per_gw.index or by_id.loc[i, "status"] not in OUT_STATUSES:
            continue
        chance = by_id.loc[i, "chance"]
        if chance == chance and chance > 0:     # NaN (no figure) counts as ruled out
            continue
        missed = 0
        for g in gws:
            if abs(per_gw.loc[i, g]) > 1e-6:
                break
            missed += 1
        if missed >= 2:
            out[i] = f"GW{gw}–{gws[missed - 1]}" + ("+" if missed == len(gws) else "")
    return out


def plan_this_week(per_gw, players, team, gw, settings=optimizer.Settings()):
    """The plan to act on, the plan that waits a week, how much better acting is, how many of the full
    plan's moves were held back, and the rule-out sales.

    Rule-out rule (your choice, Nov 26): a player FPL rules out for this gameweek and the next is sold
    now with a free transfer, unless the model would rather keep him (he's still, or again, in the best
    plan's squad at the end of the horizon). Those sales don't have to clear the bar.

    Every other transfer made now has to earn executor.GAIN_PER_TRANSFER on its own: the best plan with
    k transfers this week must beat the best plan with k-1 by that much. If the last one doesn't, the
    plan with one fewer is tried, so a weak move can't ride along with a strong one."""
    args = (per_gw, players, team.squad, team.bank, team.free_transfers, team.sell_prices)
    wait = optimizer.plan(*args, replace(settings, frozen_gws={gw}))
    if not wait.weeks:
        raise RuntimeError(f"Optimizer found no plan: {wait.status}")
    if not team.transfers_open:              # a Wildcard or Free Hit is active: transfers are yours
        return ThisWeek(wait, wait, 0.0, 0, {}, {})
    best = optimizer.plan(*args, settings)

    out = {i: span for i, span in ruled_out(per_gw, players, team.squad, gw).items() if i not in settings.locked}
    final_squad = set(best.weeks[-1].xi) | set(best.weeks[-1].bench)   # sold and bought back = worth keeping
    sold, kept = {}, {}
    for i in sorted(out, key=lambda i: per_gw.loc[i].sum()):    # least worth keeping first
        if len(sold) >= team.free_transfers:
            kept[i] = f"ruled out {out[i]}, but there's no free transfer left for him this week"
        elif i in final_squad:
            kept[i] = f"ruled out {out[i]}, but the model expects more from keeping him than from replacing him"
        else:
            sold[i] = out[i]
    floor = wait
    if sold:
        forcing = replace(settings, must_sell={**settings.must_sell, gw: set(sold)})
        only_sales = optimizer.plan(*args, replace(forcing, transfer_limit={gw: len(sold)}))
        with_sales = optimizer.plan(*args, forcing)
        if only_sales.weeks and with_sales.weeks:
            settings, floor, best = forcing, only_sales, with_sales
        else:                                # can't be done (e.g. no affordable replacement): leave it
            kept.update({i: f"ruled out {span}, but no replacement fits the budget" for i, span in sold.items()})
            sold = {}

    wanted = len(best.weeks[0].transfers_in)
    bar = executor.GAIN_PER_TRANSFER
    while len(best.weeks[0].transfers_in) > len(sold):
        count = len(best.weeks[0].transfers_in)
        extra = count - len(sold)
        fewer = floor if extra == 1 else optimizer.plan(*args, replace(settings, transfer_limit={gw: count - 1}))
        # the last move earns its keep, and the whole set beats waiting by the bar for every move
        if best.objective - fewer.objective >= bar and best.objective - floor.objective >= bar * extra:
            break
        best = fewer
    return ThisWeek(best, wait, best.objective - floor.objective, wanted - len(best.weeks[0].transfers_in),
                    sold, kept)


def analyse(per_gw, players, team, horizon_gws, agreed=None):
    """per_gw: projections up to the chips' expiry (at least the horizon). The plan uses the horizon.
    agreed: {chip: gameweek} you agreed to plan around (chips.load_plan): the plan sets them up."""
    gw = horizon_gws[0]
    agreed = agreed or {}
    plan_xp = per_gw[horizon_gws]
    base, wait, gain, held, sold, kept = plan_this_week(plan_xp, players, team, gw,
                                                        chip_planner.with_plan(optimizer.Settings(), agreed, gw))
    chips, expires = team.chips_available(gw)
    found = chip_planner.values(per_gw, players, team, chips, expires, horizon_gws) if chips else None
    table = found.table if found else pd.DataFrame()
    pairs = found.pairs if found else {}
    proposal, spread = chip_planner.propose(table, chips, gw, expires, pairs, agreed)
    value = chip_planner.value_now(table, spread, proposal, gw, pairs) if proposal else float("nan")
    return Analysis(base, wait, gain, held, chips, expires, table, spread, proposal, value, sold, kept,
                    chip_pairs=pairs, unprepared=found.unprepared if found else pd.DataFrame(), agreed=agreed)


def chip_offers(analysis, plan_xp, players, team, gw, week, agreed, declined, settings):
    """The chip weeks to ask about planning around (your choice, Nov 27): Bench Boost or Triple Captain
    in a later week of the spread, when setting up for it would change this week's transfers (else it
    can wait). week: what the main run would do this week without it. settings: the main run's (no hits).
    Not offered: a week you said no to, or a move of a week you agreed to by less than chips.MOVE_BY.
    Returns decisions for the preview: {kind: "commit", chip, gw, from, text}."""
    by_id = players.set_index("id")
    moves = lambda w: sorted(zip(w.transfers_out, w.transfers_in))
    said = lambda w: ", ".join(f"{by_id.loc[o, 'web_name']} → {by_id.loc[i, 'web_name']}"
                               for o, i in moves(w)) or "no transfers"
    offers = []
    for chip in chip_planner.PLANNED:
        target, current = analysis.chip_plan.get(chip), agreed.get(chip)
        if target is None or target <= gw or target not in plan_xp.columns or target == current \
                or (chip, target) in declined:
            continue
        table = analysis.chip_table
        if current is not None and table.loc[target, chip] - table.loc[current, chip] < chip_planner.MOVE_BY:
            continue
        prepared = plan_this_week(plan_xp, players, team, gw,
                                  chip_planner.with_plan(settings, {**agreed, chip: target}, gw)).base.weeks[0]
        if moves(prepared) == moves(week):
            continue
        name = chip_planner.NAMES[chip]
        plain = analysis.unprepared.loc[target, chip] if not analysis.unprepared.empty else float("nan")
        ask = (f"Move the {name} plan from GW{current} to GW{target}?" if current is not None
               else f"Plan around {name} in GW{target}?")
        offers.append({"kind": "commit", "chip": chip, "gw": int(target), "from": current, "text": (
            f"{ask} Setting the team up for it makes it worth about {table.loc[target, chip]:+.1f} expected points "
            f"(about {plain:+.1f} as the team stands). This week that means {said(prepared)} instead of "
            f"{said(week)}. You'll still be asked to play it in GW{target}.")})
    return offers


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
    sold, kept = analysis.sold or {}, analysis.kept or {}
    if first.transfers_in:
        rows = [[[f"{by_id.loc[o, 'web_name']} £{by_id.loc[o, 'price']:.1f}",
                  *([doc.Badge(f"out {sold[o]}", "bad")] if o in sold else [])], "→",
                 f"{by_id.loc[i, 'web_name']} £{by_id.loc[i, 'price']:.1f}", by_id.loc[i, "position"],
                 f"{per_gw.loc[o, horizon_gws].sum():.1f} → {per_gw.loc[i, horizon_gws].sum():.1f}"]
                for o, i in zip(first.transfers_out, first.transfers_in)]
        lines = [["🔁 ", doc.Tone(by_id.loc[o, "web_name"], bold=True), f" £{by_id.loc[o, 'price']:.1f}",
                  *([doc.Badge(f"out {sold[o]}", "bad")] if o in sold else []), "  →  ",
                  doc.Tone(by_id.loc[i, "web_name"], bold=True), f" £{by_id.loc[i, 'price']:.1f}",
                  f"  ·  {by_id.loc[i, 'position']}  ·  {per_gw.loc[o, horizon_gws].sum():.1f} → "
                  f"{per_gw.loc[i, horizon_gws].sum():.1f} xP, {span}"]
                 for o, i in zip(first.transfers_out, first.transfers_in)]
        blocks.append(doc.Table(["Out", "", "In", "Pos", f"xP, {span}"], rows, right={4}, slack=lines))
        for o in [o for o in first.transfers_out if o in sold]:
            news = by_id.loc[o, "news"] or "ruled out by FPL"
            blocks.append(doc.Para([doc.Tone(by_id.loc[o, "web_name"], bold=True),
                                    f" goes now: {news}, so he misses {sold[o]}. Your rule: a player ruled out "
                                    "for 2+ gameweeks is sold with a free transfer unless he's worth keeping, "
                                    "freeing his squad spot before his price drops."]))
        extra = len(first.transfers_in) - len([o for o in first.transfers_out if o in sold])
        if extra:
            lead = [f"The other move{'s' if extra > 1 else ''}: "] if sold else []
            blocks.append(doc.Para(lead + [doc.Tone(f"{analysis.gain:+.1f} expected points", "good", bold=True),
                                           f" over the horizon vs. waiting a week (each move adds at least "
                                           f"{executor.GAIN_PER_TRANSFER:.0f})."]))
    else:
        blocks.append(doc.Para("None this week" + (": banking the free transfer." if team.free_transfers else "."),
                               tone="muted"))
    for i, why in kept.items():
        blocks.append(doc.Para([doc.Tone(by_id.loc[i, "web_name"], bold=True), f" stays for now: {why}."],
                               tone="muted"))
    if analysis.held_back:
        blocks.append(doc.Para(f"{analysis.held_back} more move(s) in the full plan added less than "
                               f"{executor.GAIN_PER_TRANSFER:.0f} expected point each over waiting, so they wait.",
                               tone="muted"))
    blocks += club_limit_blocks(analysis.base, players, teams, team)
    blocks += fixture_blocks(analysis.base, per_gw, pf, players, teams, team)

    blocks.append(doc.Heading(f"Starting XI for GW{first.gw}: {first.xp:.1f} expected with captain"))
    blocks += lineup_blocks(first, per_gw, pf, players, teams)

    blocks.append(doc.Heading("Plan for the following weeks"))
    rows, lines = [], []
    for week in base.weeks[1:]:
        moves = ", ".join(f"{by_id.loc[o, 'web_name']} → {by_id.loc[i, 'web_name']}"
                          for o, i in zip(week.transfers_out, week.transfers_in)) or doc.Tone("no transfers", "muted")
        moves = [moves, doc.Badge(f"{week.hits} hit", "warn")] if week.hits else moves
        rows.append([f"GW{week.gw}", moves, by_id.loc[week.captain, "web_name"], f"{week.xp:.1f}"])
        lines.append([doc.Tone(f"GW{week.gw}", bold=True), "   ", moves,
                      f"  ·  captain {by_id.loc[week.captain, 'web_name']}  ·  {week.xp:.1f} xP"])
    blocks.append(doc.Table(["", "Transfers", "Captain", "xP"], rows, right={3},
                            note="Re-planned every run, so later weeks can change.",
                            slack=lines + [["_Re-planned every run, so later weeks can change._"]]))
    if analysis.chips:
        blocks += chip_planner.describe(analysis.chip_table, analysis.chip_plan, analysis.chip_proposal,
                                        left=f"Left this half: {chips_left} (they expire after GW{analysis.expires}).",
                                        agreed=analysis.agreed, pairs=analysis.chip_pairs)
    notes = fixture_counts(fixtures, teams, horizon_gws)
    if notes:
        blocks.append(doc.Para("Doubles and blanks: " + " | ".join(notes)))
    blocks += top_projections(per_gw[horizon_gws], players)
    return blocks


def club_limit_blocks(plan, players, teams, team):
    """What the soft club limit did: triple-ups this week's moves end, and ones the plan keeps."""
    by_id = players.set_index("id")
    short = teams.set_index("id")["short_name"]
    count = lambda ids: Counter(by_id.loc[list(ids), "team"])
    before, weeks = count(team.squad), [count(w.xi + w.bench) for w in plan.weeks]
    notes = [f"This week's moves end your {short[t]} triple-up." for t, n in before.items()
             if n >= 3 and weeks[0][t] < 3]
    for t, n in weeks[0].items():
        if n < 3:
            continue
        names = ", ".join(by_id.loc[i, "web_name"] for i in plan.weeks[0].xi + plan.weeks[0].bench
                          if by_id.loc[i, "team"] == t)
        drop = next((w.gw for w, c in zip(plan.weeks, weeks) if c[t] < 3), None)
        notes.append(f"3 from {short[t]} ({names}): " + (f"the plan drops to 2 in GW{drop}." if drop else
                     "kept through the plan, so the third is worth more than the limit's cost."))
    if not notes:
        return []
    return [doc.Para([doc.Tone("Club limit: ", bold=True), " ".join(notes),
                      " (A third player from one club has to add 2+ expected points over 6 GWs.)"], tone="muted")]


FIXTURE_WEEKS = 3


def fixture_tone(row, mu):
    """Green for an easier fixture than average for this player, red for a harder one: his team's
    expected goals for MID/FWD, the opponent's for GK/DEF (the model's team ratings, home advantage in)."""
    if row.position in ("GK", "DEF"):
        ratio = row.lam_against / mu
        return "good" if ratio <= 0.8 else "bad" if ratio >= 1.2 else None
    ratio = row.lam_for / mu
    return "good" if ratio >= 1.2 else "bad" if ratio <= 0.8 else None


def fixture_blocks(plan, per_gw, pf, players, teams, team, weeks=FIXTURE_WEEKS):
    """Your squad and the players the plan brings in: each one's next few fixtures and expected points."""
    gws = [g for g in per_gw.columns if g >= plan.weeks[0].gw][:weeks]
    if not gws or pf is None or pf.empty:
        return []
    by_id = players.set_index("id")
    short = teams.set_index("id")["short_name"]
    order = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
    games = pf[pf.gw.isin(gws)]
    mu = games.drop_duplicates(["gw", "team", "opp"]).lam_for.mean()
    first = plan.weeks[0]
    window = [w for w in plan.weeks[1:] if w.gw in gws]         # later moves whose fixtures are in view
    sell_week = {i: w.gw for w in reversed(window) for i in w.transfers_out}
    buy_week = {i: w.gw for w in reversed(window) for i in w.transfers_in}

    slack_rows = [["Player", "Pos", *[f"GW{g}" for g in gws], "xP"]]

    def row(i, badges):
        cells = [[by_id.loc[i, "web_name"], *badges], by_id.loc[i, "position"]]
        slack_row = [doc.text_of(cells[0]), by_id.loc[i, "position"]]   # Slack: a dot for each fixture's colour
        for g in gws:
            fx = games[(games.id == i) & (games.gw == g)]
            if fx.empty:
                cells.append(doc.Tone("no match", "muted"))
                slack_row.append("no match")
                continue
            parts, slack_parts = [], []
            for n, f in enumerate(fx.itertuples()):
                opp, tone = f"{short[f.opp]} ({'H' if f.home else 'A'})", fixture_tone(f, mu)
                parts += ([", "] if n else []) + [doc.Tone(opp, tone)]
                slack_parts.append(f"{doc.dot(tone)} {opp}")
            out = (fx.avail <= 0).all()
            parts += [" ", doc.Tone("out", "bad", bold=True) if out else f"{per_gw.loc[i, g]:.1f}"]
            cells.append(parts)
            slack_row.append(", ".join(slack_parts) + (" out" if out else f" {per_gw.loc[i, g]:.1f}"))
        cells.append(f"{per_gw.loc[i, gws].sum():.1f}")
        slack_rows.append(slack_row + [cells[-1]])
        return cells

    def key(i):
        return order[by_id.loc[i, "position"]], -per_gw.loc[i, gws].sum()

    rows, tones = [], {}
    for i in sorted(team.squad, key=key):
        if i in first.transfers_out:
            tones[len(rows)] = "bad"
            badges = [doc.Badge("OUT", "bad")]
        else:
            badges = [doc.Badge(f"sell GW{sell_week[i]}", "muted")] if i in sell_week else []
        rows.append(row(i, badges))
    for i in sorted(first.transfers_in, key=key):
        tones[len(rows)] = "good"
        rows.append(row(i, [doc.Badge("IN", "good")]))
    for i in sorted([i for i in buy_week if i not in team.squad and i not in first.transfers_in],
                    key=lambda i: (buy_week[i], *key(i))):
        rows.append(row(i, [doc.Badge(f"buy GW{buy_week[i]}", "muted")]))
    span = f"GW{gws[0]}–{gws[-1]}" if len(gws) > 1 else f"GW{gws[0]}"
    return [doc.Heading(f"Fixtures, {span}: your squad and the plan's buys"),
            doc.Table(["Player", "Pos", *[f"GW{g}" for g in gws], "xP"], rows, right={len(gws) + 2},
                      row_tones=tones,
                      note="Opponent in green: an easier fixture than average for him; red: harder (his team's "
                           "attack for midfielders and forwards, the opponent's attack for keepers and defenders). "
                           "Number: his expected points that week. 'out': ruled out.",
                      slack=[[f"_{doc.dot('good')} easier  {doc.dot(None)} average  {doc.dot('bad')} harder fixture. "
                              "The number after each: his expected points that week; xP: the 3 weeks'._"]],
                      slack_rows=slack_rows)]


def lineup_blocks(week, per_gw, pf, players, teams, chip=None):
    """The XI (keeper first, then by position) with opponents and captain badges, then the bench."""
    by_id = players.set_index("id")
    short = teams.set_index("id")["short_name"]
    order = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
    rows, tones, by_position, flagged = [], {}, {}, []
    for n, i in enumerate(sorted(week.xi, key=lambda i: (order[by_id.loc[i, "position"]], -per_gw.loc[i, week.gw]))):
        fx = pf[(pf.id == i) & (pf.gw == week.gw)]
        opp = ", ".join(f"{short[o]} ({'H' if h else 'A'})" for o, h in zip(fx.opp, fx.home)) or "no match"
        badge = [doc.Badge("TC" if chip == "3xc" else "C")] if i == week.captain else \
            [doc.Badge("VC", "muted")] if i == week.vice_captain else []
        name = [by_id.loc[i, "web_name"], *badge]
        # Slack: a player a line, "`MID`  Saka `C`  7.2  ARS (H)"; flagged players' news under the XI.
        player = [doc.Badge(by_id.loc[i, "position"]), " ", doc.Tone(by_id.loc[i, "web_name"], bold=True), *badge,
                  f"  {per_gw.loc[i, week.gw]:.1f}  ", f"_{opp}_"]
        if by_id.loc[i, "status"] != "a":
            news = by_id.loc[i, "news"] or by_id.loc[i, "status"]
            name += [" ", doc.Tone(news, "warn")]
            tones[n] = "warn"
            player.insert(-1, f"{doc.DOTS['warn']}  ")
            flagged.append([f"{doc.DOTS['warn']} ", doc.Tone(by_id.loc[i, "web_name"], bold=True), f": {news}"])
        rows.append([name, by_id.loc[i, "position"], opp, f"{per_gw.loc[i, week.gw]:.1f}"])
        by_position.setdefault(by_id.loc[i, "position"], []).append(player)
    lines = [p for players in by_position.values() for p in players] + ([[""]] + flagged if flagged else [])
    bench = " · ".join(f"{by_id.loc[i, 'web_name']} {per_gw.loc[i, week.gw]:.1f}" for i in week.bench)
    return [doc.Table(["Player", "Pos", "Opponent", "xP"], rows, right={3}, row_tones=tones, slack=lines),
            doc.Para([doc.Tone("Bench" + (" (Bench Boost: they count)" if chip == "bboost" else "") + ": ", bold=True),
                      bench])]


def top_projections(per_gw, players, count=6):
    by_id = players.set_index("id")
    total = per_gw.sum(axis=1)
    rows, slack_rows = [], [["Pos", "Player", "Price", "xP"]]
    for pos in ["GK", "DEF", "MID", "FWD"]:
        best = total.reindex(players[players.position == pos].id).nlargest(count)
        rows.append([pos, " · ".join(f"{by_id.loc[i, 'web_name']} £{by_id.loc[i, 'price']:.1f} ({v:.1f})"
                                    for i, v in best.items())])
        slack_rows += [[pos, by_id.loc[i, "web_name"], f"£{by_id.loc[i, 'price']:.1f}", f"{v:.1f}"]
                       for i, v in best.items()]
    return [doc.Heading(f"Top projections, GW{per_gw.columns[0]}-{per_gw.columns[-1]}"),
            doc.Table(["", "Expected points over the horizon"], rows, slack_rows=slack_rows)]


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
        mailer.send(f"GW{next_gw} plan (deadline {deadline})", doc.to_text(message), doc.to_html(message),
                    slack_posts=doc.to_slack(message))


if __name__ == "__main__":
    main()
