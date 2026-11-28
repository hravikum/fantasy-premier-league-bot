"""When to play each chip. Every chip needs your OK (a Yes in the preview).

For each chip still available this half, the extra expected points it would bring in each gameweek
up to its expiry:
  Bench Boost, Triple Captain, Wildcard   the 6-week plan re-made with the chip in that week, against
        the plan with no chip: so the value includes setting the team up for it (a bench that plays,
        the captain worth tripling, the squad a Wildcard builds). Weeks past the plan's 6 can't be
        re-planned: Bench Boost and Triple Captain then use the planned squad's bench and captain,
        and the Wildcard isn't valued until the week comes into the plan.
  Free Hit    the best one-week squad, minus the plan's team that week
A Wildcard followed by a Bench Boost is valued together too (the Wildcard builds the bench).
Chips are then spread over different gameweeks (one chip a week) to get the most points in total.
A chip is proposed when its best gameweek is the coming one and it's clearly worth playing (MIN_WORTH),
or when there are about as many chips left as gameweeks before they expire: use it or lose it.
Projections more than a few weeks out are rough, so early proposals need a big edge.

Planning around a chip (your choice, Nov 27): when the spread puts Bench Boost or Triple Captain in a
later week and setting up for it would change this week's transfers, the preview asks once whether to
plan around it. After a yes (state/chip_plan.json) the plan counts the chip in that week, so the free
transfers until then set it up; the chip itself still gets its own Yes in its week. A no isn't asked
again for that week.
"""
import json
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple

import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment

import optimizer

NAMES = {"wildcard": "Wildcard", "freehit": "Free Hit", "bboost": "Bench Boost", "3xc": "Triple Captain"}
MIN_WORTH = {"3xc": 7.0, "bboost": 15.0, "freehit": 10.0, "wildcard": 12.0}
FIELD = {"bboost": "bench_boost_gw", "3xc": "triple_captain_gw", "wildcard": "wildcard_gw"}   # optimizer.Settings
PLANNED = ("bboost", "3xc")          # chips the plan can set up for, once you agree
MOVE_BY = 2.0                        # a week you agreed to is only offered a move for this much more
PLAN = Path(__file__).with_name("state") / "chip_plan.json"


class Values(NamedTuple):
    table: pd.DataFrame           # gameweek x chip: extra expected points if played that week
    unprepared: pd.DataFrame      # Bench Boost and Triple Captain on the plan as it is (no setting up)
    pairs: dict                   # (Wildcard week, Bench Boost week): the two together
    ref: optimizer.Plan           # the plan with no chip


def squad_at(squad, plan, gw):
    current = list(squad)
    for week in plan.weeks:
        if week.gw > gw:
            break
        current = [p for p in current if p not in week.transfers_out] + week.transfers_in
    return current


def points(plan, settings):
    """A plan's expected points over its gameweeks, not discounted: less hits, plus the free transfers
    it banks (worth settings.ft_value each, as in the optimizer)."""
    return (sum(w.xp for w in plan.weeks) - settings.hit_cost * sum(w.hits for w in plan.weeks)
            + settings.ft_value * plan.banked)


def values(per_gw, players, team, chips, expires, horizon_gws, settings=optimizer.Settings()):
    """Each available chip's extra expected points per gameweek up to expiry (module docstring)."""
    plan_xp = per_gw[horizon_gws]
    args = (players, team.squad, team.bank, team.free_transfers, team.sell_prices)
    ref = optimizer.plan(plan_xp, *args, settings)
    if not ref.weeks:
        raise RuntimeError(f"Optimizer found no plan: {ref.status}")
    base = points(ref, settings)

    def worth(**chip_weeks):
        plan = optimizer.plan(plan_xp, *args, replace(settings, **chip_weeks))
        return points(plan, settings) - base if plan.weeks else float("nan")

    planned = {w.gw: w for w in ref.weeks}
    last = ref.weeks[-1]
    rows, plain = [], []
    for gw in [g for g in per_gw.columns if g <= expires]:
        squad = squad_at(team.squad, ref, gw)
        bank = planned[gw].bank if gw in planned else last.bank
        week = planned.get(gw) or optimizer.plan(per_gw[[gw]], players, squad, bank, 0, team.sell_prices,
                                                 optimizer.Settings(frozen_gws={gw})).weeks[0]
        as_is = {"3xc": per_gw.loc[week.captain, gw], "bboost": sum(per_gw.loc[i, gw] for i in week.bench)}
        row, raw = {"gw": gw}, {"gw": gw}
        for chip in chips:
            if chip == "freehit":
                free_hit = optimizer.plan(per_gw[[gw]], players, squad, bank, 0, team.sell_prices,
                                          optimizer.Settings(wildcard_gw=gw, ft_value=0))
                row[chip] = free_hit.weeks[0].xp - week.xp if free_hit.weeks else float("nan")
            elif gw in planned:
                row[chip] = worth(**{FIELD[chip]: gw})
            else:
                row[chip] = as_is.get(chip, float("nan"))     # past the plan: no Wildcard value yet
            if chip in PLANNED:
                raw[chip] = as_is[chip]
        rows.append(row)
        plain.append(raw)
    pairs = {}
    if "wildcard" in chips and "bboost" in chips:
        weeks = [g for g in horizon_gws if g <= expires]
        pairs = {(w, b): worth(wildcard_gw=w, bench_boost_gw=b) for w in weeks for b in weeks if b > w}
    frame = lambda r: pd.DataFrame(r).set_index("gw") if r else pd.DataFrame()
    return Values(frame(rows), frame(plain), {k: v for k, v in pairs.items() if v == v}, ref)


def _assign(table, chips, gws):
    """chip -> gameweek for these chips and weeks, one chip a week, most points (NaN: can't)."""
    if not chips or not gws:
        return {}
    m = table.loc[gws, chips].to_numpy(dtype=float).T            # chips x weeks
    rows, cols = linear_sum_assignment(np.where(np.isnan(m), 1e9, -m))
    return {chips[r]: gws[c] for r, c in zip(rows, cols) if not np.isnan(m[r, c])}


def score(table, plan, pairs=None):
    """The spread's total: each chip's value in its week, a Wildcard → Bench Boost pair valued together."""
    total = sum(table.loc[g, c] for c, g in plan.items())
    key = (plan.get("wildcard"), plan.get("bboost"))
    if pairs and key in pairs:
        total += pairs[key] - table.loc[key[0], "wildcard"] - table.loc[key[1], "bboost"]
    return total


def assign(table, pairs=None, fixed=None):
    """chip -> gameweek, one chip a week, most points in total. pairs: {(Wildcard week, Bench Boost
    week): value together}. fixed: {chip: gameweek} you agreed to plan around, kept as they are."""
    table = table.dropna(axis=1, how="all")
    if table.empty:
        return {}
    fixed = {c: g for c, g in (fixed or {}).items() if c in table.columns and g in table.index}
    pairs = pairs if {"wildcard", "bboost"} <= set(table.columns) else {}

    def complete(fix):
        rest = [c for c in table.columns if c not in fix]
        return {**_assign(table, rest, [g for g in table.index if g not in fix.values()]), **fix}

    candidates = [complete(fixed)]
    for w, b in pairs or {}:
        taken = {g for c, g in fixed.items() if c not in ("wildcard", "bboost")}
        if fixed.get("wildcard", w) == w and fixed.get("bboost", b) == b and not {w, b} & taken:
            candidates.append(complete({**fixed, "wildcard": w, "bboost": b}))
    return max(candidates, key=lambda plan: score(table, plan, pairs))


def value_now(table, plan, chip, gw, pairs=None):
    """What playing the chip this week is worth: a Wildcard that sets up a planned Bench Boost counts
    the pair's value beyond the Bench Boost's own."""
    b = plan.get("bboost")
    if chip == "wildcard" and pairs and (gw, b) in pairs:
        return pairs[(gw, b)] - table.loc[b, "bboost"]
    return table.loc[gw, chip]


def propose(table, chips, gw, expires, pairs=None, agreed=None):
    """The chip to ask about for this gameweek (or None), and the plan for the rest. A chip week you
    agreed to plan around is asked about when it comes (if the chip is still worth something)."""
    agreed = agreed or {}
    plan = assign(table, pairs, agreed)
    if table.empty or gw not in table.index:
        return None, plan
    for chip, week in agreed.items():
        if week == gw and chip in table.columns and table.loc[gw, chip] > 0:
            return chip, plan
    urgent = expires - gw + 1 <= len(chips) + 1
    now = [c for c, when in plan.items()
           if when == gw and (urgent or value_now(table, plan, c, gw, pairs) >= MIN_WORTH[c])]
    return (max(now, key=lambda c: value_now(table, plan, c, gw, pairs)) if now else None), plan


# ---------------------------------------------------------------- the chip weeks you agreed to

def load_plan(team, gw):
    """({chip: gameweek} you agreed to plan around, {(chip, gameweek)} you said no to), without the
    ones that are past or whose chip is gone (played, or expired)."""
    raw = json.loads(PLAN.read_text()) if PLAN.exists() else {}
    available, expires = team.chips_available(gw)
    agreed = {c: int(g) for c, g in raw.get("agreed", {}).items() if c in available and gw <= int(g) <= expires}
    declined = {(c, int(g)) for c, g in raw.get("declined", []) if int(g) >= gw}
    return agreed, declined


def save_plan(agreed, declined):
    PLAN.parent.mkdir(exist_ok=True)
    PLAN.write_text(json.dumps({"agreed": agreed, "declined": sorted([c, g] for c, g in declined)}, indent=1))


def with_plan(settings, agreed, gw):
    """The optimizer's settings with the chip weeks you agreed to, after this one: the plan sets them
    up. This week's chip is only ever played after its own Yes."""
    return replace(settings, **{FIELD[c]: g for c, g in agreed.items()
                                if g > gw and getattr(settings, FIELD[c]) is None})


def describe(table, plan, proposal, left="", agreed=None, pairs=None):
    """Email blocks: each chip's value per gameweek, the best spread, and this week's proposal."""
    import doc
    agreed = agreed or {}
    blocks = [doc.Heading("Chips")] + ([doc.Para(left, tone="muted")] if left else [])
    if not table.empty:
        chips = list(table.columns)
        best = {c: table[c].idxmax() for c in chips if table[c].notna().any()}
        rows = []
        for gw, row in table.iterrows():
            cells = [f"GW{gw}"]
            for c in chips:
                text = "–" if row[c] != row[c] else f"{row[c]:.1f}"
                cells.append([doc.Tone(text, "good", bold=True), doc.Badge("plan", "good")] if plan.get(c) == gw
                             else doc.Tone(text, bold=True) if best.get(c) == gw else text)
            rows.append(cells)
        first = table.index[0]
        lines = []                  # Slack: a line a chip, its value now and the week the plan uses it
        for c in chips:
            now = table.loc[first, c]
            line = [doc.Tone(NAMES[c], bold=True), f"   GW{first} " + ("–" if now != now else f"{now:+.1f}")]
            if c in plan:
                line += ["  ·  ", doc.Tone(f"plan: GW{plan[c]} {table.loc[plan[c], c]:+.1f}", bold=True)]
            elif c in best and best[c] != first:
                line.append(f"  ·  best: GW{best[c]} {table.loc[best[c], c]:+.1f}")
            lines.append(line)
        blocks.append(doc.Table(["", *[NAMES[c] for c in chips]], rows, right=set(range(1, len(chips) + 1)),
                                note="Extra expected points if played that week, with the team set up for it "
                                     "(within the 6-week plan). 'plan' marks the best spread, one chip a week, "
                                     "before they expire. –: past the plan, not valued yet.", slack=lines))
    w, b = plan.get("wildcard"), plan.get("bboost")
    if pairs and (w, b) in pairs:
        blocks.append(doc.Para(f"Wildcard in GW{w} then Bench Boost in GW{b}: {pairs[(w, b)]:+.1f} expected "
                               f"points together (the Wildcard builds the bench)."))
    for chip, week in sorted(agreed.items(), key=lambda kv: kv[1]):
        blocks.append(doc.Para([doc.Tone("Planned with you: ", bold=True),
                                f"{NAMES[chip]} in GW{week}. The bot's transfers until then set it up; "
                                f"you'll still be asked to play it that week."]))
    blocks.append(doc.Para([doc.Tone("Proposed this week: ", bold=True), NAMES[proposal]] if proposal
                           else "No chip proposed this week: none is clearly worth it yet.",
                           tone=None if proposal else "muted"))
    return blocks
