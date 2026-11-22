"""Optimizer tests on a small made-up player pool.  python3 test_optimizer.py  (or pytest)"""
from collections import Counter

import numpy as np
import pandas as pd

import optimizer

RNG = np.random.default_rng(7)
GWS = [10, 11, 12]


def make_pool():
    rows, pid = [], 1
    for pos, count in {"GK": 6, "DEF": 16, "MID": 16, "FWD": 10}.items():
        for k in range(count):
            rows.append({"id": pid, "web_name": f"{pos}{k}", "position": pos, "team": pid % 10 + 1,
                         "price": round(4.0 + RNG.uniform(0, 6), 1)})
            pid += 1
    players = pd.DataFrame(rows)
    xp = pd.DataFrame({gw: players["price"] * 0.6 + RNG.normal(0, 0.5, len(players)) for gw in GWS})
    xp.index = players["id"]
    return players, xp.clip(lower=0)


def cheapest_squad(players):
    """A legal squad (quotas, 3 per club) of the cheapest players."""
    squad, clubs = [], Counter()
    for pos, quota in optimizer.QUOTA.items():
        for row in players[players.position == pos].sort_values("price").itertuples():
            if sum(players.set_index("id").loc[squad, "position"] == pos) < quota and clubs[row.team] < 3:
                squad.append(row.id)
                clubs[row.team] += 1
    return squad


def check_rules(week, players, squad):
    by_id = players.set_index("id")
    assert len(squad) == 15
    assert Counter(by_id.loc[squad, "position"]) == Counter(optimizer.QUOTA)
    assert max(Counter(by_id.loc[squad, "team"]).values()) <= 3
    xi = by_id.loc[week.xi, "position"]
    assert len(week.xi) == 11 and (xi == "GK").sum() == 1
    assert 3 <= (xi == "DEF").sum() <= 5 and 1 <= (xi == "FWD").sum() <= 3
    assert set(week.xi) <= set(squad) and set(week.bench) == set(squad) - set(week.xi)
    assert by_id.loc[week.bench[0], "position"] == "GK"
    assert week.captain in week.xi and week.vice_captain in week.xi and week.captain != week.vice_captain
    assert week.bank >= -1e-6


def replay(plan, squad):
    """Apply a plan's transfers week by week and return the squads it implies."""
    squads, current = [], list(squad)
    for week in plan.weeks:
        current = [p for p in current if p not in week.transfers_out] + week.transfers_in
        squads.append(current)
    return squads


def test_rules_hold_every_week():
    players, xp = make_pool()
    squad = cheapest_squad(players)
    result = optimizer.plan(xp, players, squad, bank=15.0, free_transfers=2)
    assert result.weeks, result.status
    for week, current in zip(result.weeks, replay(result, squad)):
        check_rules(week, players, current)
        assert len(week.transfers_in) == len(week.transfers_out)
        assert len(week.transfers_in) <= week.free_transfers + week.hits


def test_buys_an_obvious_upgrade_with_a_free_transfer():
    players, xp = make_pool()
    squad = cheapest_squad(players)
    star = players[(players.position == "MID") & ~players.id.isin(squad)].iloc[0]["id"]
    xp.loc[star] = 15.0
    players.loc[players.id == star, "price"] = 5.0
    result = optimizer.plan(xp, players, squad, bank=1.0, free_transfers=1)
    assert star in result.weeks[0].transfers_in
    assert result.weeks[0].hits == 0
    assert result.weeks[0].captain == star


def test_no_hit_for_a_small_gain():
    players, xp = make_pool()
    squad = cheapest_squad(players)
    first = optimizer.plan(xp, players, squad, bank=0.0, free_transfers=0,
                           settings=optimizer.Settings(max_hits=1))
    assert first.weeks[0].hits == 0 or first.weeks[0].xp > 0   # hits only when they pay
    # Make every non-owned player only marginally better than who they'd replace: no hit is worth -4.
    flat = xp.copy()
    flat.loc[:] = 2.0
    flat.loc[~flat.index.isin(squad)] = 2.2
    result = optimizer.plan(flat, players, squad, bank=50.0, free_transfers=0)
    assert all(w.hits == 0 for w in result.weeks)


def test_wildcard_builds_a_squad_from_nothing():
    players, xp = make_pool()
    result = optimizer.plan(xp, players, [], bank=100.0, free_transfers=0,
                            settings=optimizer.Settings(wildcard_gw=GWS[0]))
    week = result.weeks[0]
    assert len(week.transfers_in) == 15 and week.hits == 0
    check_rules(week, players, week.transfers_in)


def test_locked_and_banned_players():
    players, xp = make_pool()
    squad = cheapest_squad(players)
    worst = squad[-1]
    best_outsider = xp.loc[~xp.index.isin(squad)].sum(axis=1).idxmax()
    settings = optimizer.Settings(locked={worst}, banned={best_outsider})
    result = optimizer.plan(xp, players, squad, bank=20.0, free_transfers=5, settings=settings)
    for week in result.weeks:
        assert worst not in week.transfers_out
        assert best_outsider not in week.transfers_in


def test_bench_boost_and_triple_captain_count():
    players, xp = make_pool()
    squad = cheapest_squad(players)
    plain = optimizer.plan(xp, players, squad, bank=0.0, free_transfers=0)
    boosted = optimizer.plan(xp, players, squad, bank=0.0, free_transfers=0,
                             settings=optimizer.Settings(bench_boost_gw=GWS[0], triple_captain_gw=GWS[1]))
    bench_pts = sum(xp.loc[i, GWS[0]] for i in plain.weeks[0].bench)
    assert abs(boosted.weeks[0].xp - (plain.weeks[0].xp + bench_pts)) < 0.02
    assert abs(boosted.weeks[1].xp - (plain.weeks[1].xp + xp.loc[plain.weeks[1].captain, GWS[1]])) < 0.02


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
