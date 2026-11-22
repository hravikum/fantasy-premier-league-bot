"""Executor tests: the requests it would send and the hybrid policy.  python3 -m pytest test_executor.py"""
import executor
import optimizer
import recommend
import team
import test_optimizer as fixtures


def scenario(hits=0, free=2, open_=True, picks=None):
    players, xp = fixtures.make_pool()
    squad = fixtures.cheapest_squad(players)
    star = players[(players.position == "MID") & ~players.id.isin(squad)].iloc[0]["id"]
    xp.loc[star] = 15.0
    players.loc[players.id == star, "price"] = 4.0
    planned = optimizer.plan(xp, players, squad, 0.5, free, settings=optimizer.Settings(max_hits=2))
    waiting = optimizer.plan(xp, players, squad, 0.5, free, settings=optimizer.Settings(frozen_gws={fixtures.GWS[0]}))
    me = team.Team(42, "Test", squad, 0.5, free, {i: players.set_index("id").loc[i, "price"] for i in squad},
                   picks=picks or [], transfers_open=open_)
    return players, me, planned, waiting, star


def test_payloads_match_fpl_shapes():
    players, me, planned, waiting, star = scenario()
    week = planned.weeks[0]
    body = executor.transfers_payload(me.entry, week, players, me.sell_prices)
    assert body["entry"] == 42 and body["event"] == fixtures.GWS[0] and body["confirmed"] is True
    pos = players.set_index("id")["position"]
    for t in body["transfers"]:
        assert pos[t["element_in"]] == pos[t["element_out"]]
        assert isinstance(t["purchase_price"], int) and isinstance(t["selling_price"], int)
    picks = executor.lineup_payload(week, players)["picks"]
    assert [p["position"] for p in picks] == list(range(1, 16))
    assert pos[picks[0]["element"]] == "GK" and pos[picks[11]["element"]] == "GK"
    assert sum(p["is_captain"] for p in picks) == 1 and sum(p["is_vice_captain"] for p in picks) == 1
    order = [executor.ORDER[pos[p["element"]]] for p in picks[:11]]
    assert order == sorted(order)


def test_free_transfer_with_a_real_gain_is_automatic():
    players, me, planned, waiting, star = scenario()
    gain = planned.objective - waiting.objective
    actions = executor.decide(me, planned.weeks[0], waiting.weeks[0], gain, players)
    transfers, lineup = actions
    assert transfers.mode == "auto" and star in [t["element_in"] for t in transfers.payload["transfers"]]
    assert star in [p["element"] for p in lineup.payload["picks"]]          # lineup follows the transfers


def test_small_gain_waits_and_lineup_uses_current_squad():
    players, me, planned, waiting, star = scenario()
    n = len(planned.weeks[0].transfers_in)
    actions = executor.decide(me, planned.weeks[0], waiting.weeks[0], n * executor.GAIN_PER_TRANSFER - 0.1, players)
    assert actions[0].mode == "skip" and "waiting a week" in actions[0].why
    assert {p["element"] for p in actions[1].payload["picks"]} == set(me.squad)


def test_hits_ask_first_and_active_chip_skips():
    players, me, planned, waiting, star = scenario(free=0)
    week = planned.weeks[0]
    assert week.hits >= 1
    assert executor.decide(me, week, waiting.weeks[0], 5.0, players)[0].mode == "ask"
    players, me, planned, waiting, star = scenario(open_=False)
    assert executor.decide(me, planned.weeks[0], waiting.weeks[0], 5.0, players)[0].mode == "skip"


def test_lineup_already_right_is_left_alone():
    players, me, planned, waiting, star = scenario()
    current = executor.lineup_payload(waiting.weeks[0], players)["picks"]
    me.picks = current
    actions = executor.decide(me, planned.weeks[0], waiting.weeks[0], 0.0, players)
    assert actions[-1].mode == "skip"
    swapped = [dict(p, is_captain=p["is_vice_captain"], is_vice_captain=p["is_captain"]) for p in current]
    me.picks = swapped
    actions = executor.decide(me, planned.weeks[0], waiting.weeks[0], 0.0, players)
    assert actions[-1].mode == "auto" and "captain:" in actions[-1].why


def test_each_transfer_must_clear_the_bar():
    """A strong move and a weak one, with free transfers to spare: only the strong one is made now."""
    players, xp = fixtures.make_pool()
    squad = fixtures.cheapest_squad(players)
    outsiders = players[~players.id.isin(squad)]
    big = outsiders[outsiders.position == "MID"].iloc[0]["id"]
    small = outsiders[outsiders.position == "DEF"].iloc[0]["id"]
    flat = xp.copy()
    flat.loc[:] = 2.0
    flat.loc[big] = 8.0                       # +6 a week over any midfielder
    flat.loc[small] = 2.3                     # +0.3 a week over any defender
    players.loc[players.id.isin([big, small]), "price"] = 4.0
    me = team.Team(1, "x", squad, 10.0, 5, {i: players.set_index("id").loc[i, "price"] for i in squad})
    full = optimizer.plan(flat, players, squad, 10.0, 5, me.sell_prices)
    raw = full.weeks[0].transfers_in
    assert {big, small} <= set(raw)           # unconstrained, it makes both (plus free zero-gain swaps)
    base, wait, gain, held = recommend.plan_this_week(flat, players, me, fixtures.GWS[0])
    assert base.weeks[0].transfers_in == [big] and held == len(raw) - 1
    assert executor.decide(me, base.weeks[0], wait.weeks[0], gain, players)[0].mode == "auto"
