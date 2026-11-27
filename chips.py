"""When to play each chip. Every chip needs your OK (reply YES to the preview email).

For each chip still available this half, the extra expected points it would bring in each gameweek
up to its expiry, with the squad the plan expects to have then:
  Triple Captain  the captain's points once more        Bench Boost  the bench's points
  Free Hit        best one-week squad minus the plan's   Wildcard     re-planning from scratch now
Chips are then spread over different gameweeks (one chip a week) to get the most points in total.
A chip is proposed when its best gameweek is the coming one and it's clearly worth playing (MIN_WORTH),
or when there are about as many chips left as gameweeks before they expire: use it or lose it.
Projections more than a few weeks out are rough, so early proposals need a big edge.
"""
import itertools

import pandas as pd

import optimizer

NAMES = {"wildcard": "Wildcard", "freehit": "Free Hit", "bboost": "Bench Boost", "3xc": "Triple Captain"}
MIN_WORTH = {"3xc": 7.0, "bboost": 15.0, "freehit": 10.0, "wildcard": 12.0}


def squad_at(squad, plan, gw):
    current = list(squad)
    for week in plan.weeks:
        if week.gw > gw:
            break
        current = [p for p in current if p not in week.transfers_out] + week.transfers_in
    return current


def values(per_gw, players, team, base, chips, expires):
    """Extra expected points from each available chip (not the Wildcard) per gameweek up to expiry."""
    planned = {w.gw: w for w in base.weeks}
    last = base.weeks[-1]
    rows = []
    for gw in [g for g in per_gw.columns if g <= expires]:
        squad = squad_at(team.squad, base, gw)
        bank = planned[gw].bank if gw in planned else last.bank
        week = planned.get(gw) or optimizer.plan(per_gw[[gw]], players, squad, bank, 0, team.sell_prices,
                                                 optimizer.Settings(frozen_gws={gw})).weeks[0]
        row = {"gw": gw}
        if "3xc" in chips:
            row["3xc"] = per_gw.loc[week.captain, gw]
        if "bboost" in chips:
            row["bboost"] = sum(per_gw.loc[i, gw] for i in week.bench)
        if "freehit" in chips:
            free_hit = optimizer.plan(per_gw[[gw]], players, squad, bank, 0, team.sell_prices,
                                      optimizer.Settings(wildcard_gw=gw, ft_value=0))
            row["freehit"] = free_hit.weeks[0].xp - week.xp if free_hit.weeks else float("nan")
        rows.append(row)
    return pd.DataFrame(rows).set_index("gw") if rows else pd.DataFrame()


def wildcard_gain(per_gw, players, team, base, gw):
    """Expected points over the horizon from playing the Wildcard now instead of the plan."""
    wild = optimizer.plan(per_gw, players, team.squad, team.bank, team.free_transfers, team.sell_prices,
                          optimizer.Settings(wildcard_gw=gw))
    return wild.objective - base.objective if wild.weeks else float("nan")


def assign(table):
    """chip -> gameweek, one chip a week, most points in total."""
    chips = list(table.columns)
    gws = list(table.index)
    if not chips or not gws:
        return {}
    best, best_total = {}, float("-inf")
    for picked in itertools.permutations(gws, min(len(chips), len(gws))):
        for order in itertools.permutations(chips, len(picked)):
            total = sum(table.loc[g, c] for g, c in zip(picked, order))
            if total > best_total:
                best, best_total = dict(zip(order, picked)), total
    return best


def propose(table, chips, gw, expires, wild=float("nan")):
    """The chip to ask about for this gameweek (or None), and the plan for the rest."""
    urgent = expires - gw + 1 <= len(chips) + 1
    if "wildcard" in chips and (wild >= MIN_WORTH["wildcard"] or gw == expires):
        return "wildcard", {}
    plan = assign(table.dropna(axis=1, how="all"))
    now = [c for c, when in plan.items() if when == gw and (urgent or table.loc[gw, c] >= MIN_WORTH[c])]
    return (max(now, key=lambda c: table.loc[gw, c]) if now else None), plan


def describe(table, plan, proposal, wild=float("nan"), left=""):
    """Email blocks: each chip's value per gameweek, the best spread, and this week's proposal."""
    import doc
    blocks = [doc.Heading("Chips")] + ([doc.Para(left, tone="muted")] if left else [])
    if not table.empty:
        chips = list(table.columns)
        best = {c: table[c].idxmax() for c in chips}
        rows = []
        for gw, row in table.iterrows():
            cells = [f"GW{gw}"]
            for c in chips:
                text = f"{row[c]:.1f}"
                cells.append([doc.Tone(text, "good", bold=True), doc.Badge("plan", "good")] if plan.get(c) == gw
                             else doc.Tone(text, bold=True) if best[c] == gw else text)
            rows.append(cells)
        first = table.index[0]
        lines = []                  # Slack: a line a chip, its value now and the week the plan uses it
        for c in chips:
            line = [doc.Tone(NAMES[c], bold=True), f"   GW{first} {table.loc[first, c]:+.1f}"]
            if c in plan:
                line += ["  ·  ", doc.Tone(f"plan: GW{plan[c]} {table.loc[plan[c], c]:+.1f}", bold=True)]
            elif best[c] != first:
                line.append(f"  ·  best: GW{best[c]} {table.loc[best[c], c]:+.1f}")
            lines.append(line)
        blocks.append(doc.Table(["", *[NAMES[c] for c in chips]], rows, right=set(range(1, len(chips) + 1)),
                                note="Extra expected points if played that week. 'plan' marks the best spread, "
                                     "one chip a week, before they expire.", slack=lines))
    if wild == wild:
        blocks.append(doc.Para(f"Wildcard now: {wild:+.1f} expected points over the horizon vs. the plan."))
    blocks.append(doc.Para([doc.Tone("Proposed this week: ", bold=True), NAMES[proposal]] if proposal
                           else "No chip proposed this week: none is clearly worth it yet.",
                           tone=None if proposal else "muted"))
    return blocks
