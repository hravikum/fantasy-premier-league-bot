"""Pick transfers, starting XI, bench and captain over several gameweeks: one integer program.

Maximises discounted expected points over the horizon, less 4 per hit, plus a value for every free
transfer still banked at the end (so a transfer has to beat keeping it). Respects FPL's rules:
2 GK / 5 DEF / 5 MID / 3 FWD, at most 3 per club, 1 GK + 3-5 DEF + 1-3 FWD in the XI, budget with
selling prices, free transfers banked up to 5. Chips: Wildcard and Free Hit make one gameweek's
transfers free (Free Hit squads are planned with plan(..., gws=[gw], wildcard_gw=gw) and thrown
away after), Bench Boost counts the bench, Triple Captain triples the captain.
"""
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import LinearConstraint, milp
from scipy.sparse import lil_matrix

QUOTA = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
POOL = {"GK": 10, "DEF": 30, "MID": 30, "FWD": 20}
MAX_FREE_TRANSFERS = 5


@dataclass
class Settings:
    decay: float = 0.85            # weight of each later gameweek relative to the one before
    bench_weight: float = 0.1      # bench points count a little (autosubs)
    hit_cost: float = 4.0
    max_hits: int = 2              # per gameweek
    ft_value: float = 1.5          # points a banked free transfer is worth at the end of the horizon
    wildcard_gw: int | None = None
    bench_boost_gw: int | None = None
    triple_captain_gw: int | None = None
    locked: set = field(default_factory=set)   # never sell
    banned: set = field(default_factory=set)   # never buy
    frozen_gws: set = field(default_factory=set)   # no transfers in these gameweeks
    transfer_limit: dict = field(default_factory=dict)   # gameweek -> most transfers allowed
    forced: dict = field(default_factory=dict)   # gameweek -> (ids in, ids out): exactly these transfers
    must_sell: dict = field(default_factory=dict)   # gameweek -> ids that must go that week (any replacement)
    club_max: dict = field(default_factory=dict)    # team id -> most players from that club (FPL's limit is 3)
    defence_max: int | None = None                  # most keepers + defenders from any one club
    time_limit: float = 120.0


@dataclass
class Week:
    gw: int
    transfers_in: list
    transfers_out: list
    hits: int
    free_transfers: int            # available going into this gameweek
    bank: float                    # after this gameweek's transfers
    xi: list                       # starting XI, best first
    bench: list                    # FPL order: reserve GK, then outfield by expected points
    captain: int
    vice_captain: int
    xp: float                      # expected points of the XI, with the captain (and chips)


@dataclass
class Plan:
    weeks: list
    objective: float
    status: str


class _Vars:
    """Maps named variables to column numbers for scipy's milp."""

    def __init__(self):
        self.n = 0
        self.lo, self.hi, self.integer = [], [], []

    def add(self, count, lo=0.0, hi=1.0, integer=True):
        start = self.n
        self.n += count
        self.lo += [lo] * count
        self.hi += [hi] * count
        self.integer += [int(integer)] * count
        return np.arange(start, start + count)


def candidate_pool(xp, players, squad, settings):
    """The current squad plus the best players per position by discounted expected points."""
    weights = settings.decay ** np.arange(xp.shape[1])
    score = (xp.values * weights).sum(axis=1)
    ranked = players.assign(score=score).sort_values("score", ascending=False)
    ranked = ranked[~ranked["id"].isin(settings.banned) & (ranked["score"] > 0)]
    keep = set(squad) | {i for ins, _ in settings.forced.values() for i in ins}
    for pos, size in POOL.items():
        keep |= set(ranked[ranked["position"] == pos]["id"].head(size))
    return players[players["id"].isin(keep)].reset_index(drop=True)


def plan(xp, players, squad, bank, free_transfers, sell_prices=None, settings=Settings()):
    """xp: DataFrame indexed by player id, one column per gameweek (the horizon, in order).
    players: id, web_name, position, team, price. squad: 15 ids. bank in £m."""
    gws = list(xp.columns)
    sell_prices = sell_prices or {}
    pool = candidate_pool(xp.reindex(players["id"]).fillna(0), players, squad, settings)
    ids = pool["id"].to_numpy()
    n, T = len(ids), len(gws)
    pts = xp.reindex(ids).fillna(0).to_numpy()                 # n x T
    owned = np.isin(ids, list(squad)).astype(float)
    buy = pool["price"].to_numpy()
    sell = np.array([sell_prices.get(i, p) for i, p in zip(ids, buy)])

    v = _Vars()
    x = [v.add(n) for _ in gws]          # in squad
    s = [v.add(n) for _ in gws]          # starting
    c = [v.add(n) for _ in gws]          # captain
    tin = [v.add(n) for _ in gws]        # bought this gameweek
    tout = [v.add(n) for _ in gws]       # sold this gameweek
    ft = v.add(T + 1, 0, MAX_FREE_TRANSFERS)                    # free transfers going into each GW
    used = v.add(T, 0, MAX_FREE_TRANSFERS)                      # free transfers used
    paid = v.add(T, 0, settings.max_hits)                       # hits
    waste = v.add(T, 0, MAX_FREE_TRANSFERS)                     # free transfers lost to the cap
    wild = v.add(T, 0, 15)                                      # transfers made free by a chip
    money = v.add(T, 0, 1000, integer=False)                    # bank after each GW's transfers

    rows, lb, ub = [], [], []

    def constrain(coefs, lo, hi):
        rows.append(coefs)
        lb.append(lo)
        ub.append(hi)

    objective = np.zeros(v.n)
    for t, gw in enumerate(gws):
        weight = settings.decay ** t
        boost = gw == settings.bench_boost_gw
        objective[s[t]] -= weight * pts[:, t] * (0 if boost else 1 - settings.bench_weight)
        objective[x[t]] -= weight * pts[:, t] * (1 if boost else settings.bench_weight)
        objective[c[t]] -= weight * pts[:, t] * (2 if gw == settings.triple_captain_gw else 1)
        objective[paid[t]] += weight * settings.hit_cost

        for i in range(n):
            # squad flow: x_t = x_{t-1} + in - out
            row = {x[t][i]: 1, tin[t][i]: -1, tout[t][i]: 1}
            if t:
                row[x[t - 1][i]] = -1
            constrain(row, owned[i] if t == 0 else 0, owned[i] if t == 0 else 0)
            constrain({s[t][i]: 1, x[t][i]: -1}, -np.inf, 0)         # start only if in squad
            constrain({c[t][i]: 1, s[t][i]: -1}, -np.inf, 0)         # captain only if starting
        for pos, quota in QUOTA.items():
            members = np.where(pool["position"].to_numpy() == pos)[0]
            constrain({x[t][i]: 1 for i in members}, quota, quota)
            lo, hi = {"GK": (1, 1), "DEF": (3, 5), "MID": (2, 5), "FWD": (1, 3)}[pos]
            constrain({s[t][i]: 1 for i in members}, lo, hi)
        defensive = pool["position"].isin(["GK", "DEF"]).to_numpy()
        for team in pool["team"].unique():
            members = np.where(pool["team"].to_numpy() == team)[0]
            constrain({x[t][i]: 1 for i in members}, -np.inf, min(3, settings.club_max.get(team, 3)))
            if settings.defence_max is not None:
                constrain({x[t][i]: 1 for i in members if defensive[i]}, -np.inf, settings.defence_max)
        constrain({s[t][i]: 1 for i in range(n)}, 11, 11)
        constrain({c[t][i]: 1 for i in range(n)}, 1, 1)

        # money: bank_t = bank_{t-1} + sales - purchases
        row = {money[t]: 1}
        row.update({tout[t][i]: -sell[i] for i in range(n)})
        row.update({tin[t][i]: buy[i] for i in range(n)})
        if t:
            row[money[t - 1]] = -1
        constrain(row, bank if t == 0 else 0, bank if t == 0 else 0)

        # transfers: bought = free used + hits + chip-free; free transfers bank up to 5
        row = {tin[t][i]: 1 for i in range(n)}
        row.update({used[t]: -1, paid[t]: -1, wild[t]: -1})
        constrain(row, 0, 0)
        constrain({used[t]: 1, ft[t]: -1}, -np.inf, 0)
        constrain({ft[t + 1]: 1, ft[t]: -1, used[t]: 1, waste[t]: 1}, 1, 1)
        if gw in settings.frozen_gws:
            constrain({tin[t][i]: 1 for i in range(n)}, 0, 0)
        if gw in settings.transfer_limit:
            constrain({tin[t][i]: 1 for i in range(n)}, 0, settings.transfer_limit[gw])
        if gw in settings.forced:
            ins, outs = settings.forced[gw]
            constrain({tin[t][i]: 1 for i in range(n)}, len(ins), len(ins))
            for i in range(n):
                if ids[i] in ins:
                    constrain({tin[t][i]: 1}, 1, 1)
                if ids[i] in outs:
                    constrain({tout[t][i]: 1}, 1, 1)
        for i in range(n):
            if ids[i] in settings.must_sell.get(gw, ()):
                constrain({tout[t][i]: 1}, 1, 1)
        if gw == settings.wildcard_gw:
            constrain({paid[t]: 1}, 0, 0)
        else:
            constrain({wild[t]: 1}, 0, 0)
        for i in range(n):
            if ids[i] in settings.locked:
                constrain({x[t][i]: 1}, 1, 1)
    constrain({ft[0]: 1}, free_transfers, free_transfers)
    objective[ft[T]] -= settings.ft_value * settings.decay ** T

    A = lil_matrix((len(rows), v.n))
    for r, coefs in enumerate(rows):
        for col, val in coefs.items():
            A[r, col] = val
    result = milp(objective, constraints=LinearConstraint(A.tocsr(), lb, ub),
                  integrality=np.array(v.integer), bounds=(v.lo, v.hi),
                  options={"time_limit": settings.time_limit, "mip_rel_gap": 1e-4})
    if result.x is None:
        return Plan([], float("nan"), result.message)
    sol = np.round(result.x).astype(int)
    money_path = result.x[money]

    weeks = []
    for t, gw in enumerate(gws):
        squad_now = ids[sol[x[t]] == 1]
        xi = sorted(ids[sol[s[t]] == 1], key=lambda i: -pts[ids.tolist().index(i), t])
        bench_ids = [i for i in squad_now if i not in xi]
        pos_of = pool.set_index("id")["position"]
        bench = ([i for i in bench_ids if pos_of[i] == "GK"]
                 + sorted([i for i in bench_ids if pos_of[i] != "GK"], key=lambda i: -pts[ids.tolist().index(i), t]))
        xi = [int(i) for i in xi]
        captain = int(ids[sol[c[t]] == 1][0])
        vice = next(i for i in xi if i != captain)
        mult = 3 if gw == settings.triple_captain_gw else 2
        team_xp = sum(pts[ids.tolist().index(i), t] for i in (squad_now if gw == settings.bench_boost_gw else xi))
        team_xp += (mult - 1) * pts[ids.tolist().index(captain), t]
        outs, ins = _pair_by_position(ids[sol[tout[t]] == 1], ids[sol[tin[t]] == 1], pos_of)
        weeks.append(Week(
            gw=gw, transfers_in=ins, transfers_out=outs,
            hits=int(sol[paid[t]]), free_transfers=int(sol[ft[t]]), bank=round(float(money_path[t]), 1),
            xi=xi, bench=[int(i) for i in bench], captain=int(captain), vice_captain=int(vice), xp=round(float(team_xp), 2)))
    return Plan(weeks, -result.fun, result.message)


def _pair_by_position(outs, ins, pos_of):
    """FPL takes transfers as like-for-like pairs, so line each sale up with a purchase in the same
    position: transfers_out[k] is replaced by transfers_in[k]."""
    paired_out, paired_in = [], []
    for pos in QUOTA:
        paired_out += sorted(int(i) for i in outs if pos_of[i] == pos)
        paired_in += sorted(int(i) for i in ins if pos_of[i] == pos)
    return paired_out, paired_in
