"""Your FPL team from the public endpoints: squad, bank, free transfers, selling prices, chips.

load() uses public data, which shows the squad as of the last deadline. load_private() logs in
and reads my-team: the squad right now (with pending transfers), exact selling prices, the free
transfers left this week and the current lineup.
"""
import math
from dataclasses import dataclass, field

import data

CHIPS = {"wildcard": "Wildcard", "freehit": "Free Hit", "bboost": "Bench Boost", "3xc": "Triple Captain"}
HALVES = [(1, 19), (20, 38)]       # each chip can be played once in each half of the season


@dataclass
class Team:
    entry: int
    name: str
    squad: list
    bank: float
    free_transfers: int
    sell_prices: dict
    chips_used: list = field(default_factory=list)      # (chip, gameweek)
    picks: list = field(default_factory=list)           # the current lineup, when logged in
    transfers_open: bool = True                         # False while a Wildcard/Free Hit is active
    active_chip: str | None = None                      # a chip already played for the coming gameweek

    def chips_available(self, gw):
        first, last = next(h for h in HALVES if h[0] <= gw <= h[1])
        used = {chip for chip, event in self.chips_used if first <= event <= last}
        return [c for c in CHIPS if c not in used], last


def selling_price(bought, now):
    """FPL keeps half of any rise (rounded down to £0.1m) and passes on the whole of any fall."""
    if now <= bought:
        return now
    return round(bought + math.floor(round((now - bought) * 10)) // 2 / 10, 1)


def free_transfers(history, chips):
    """Free transfers going into the next gameweek: one a week, banked up to 5. Wildcard and Free
    Hit weeks don't use any. GW1 is unlimited, so counting starts with one going into GW2."""
    chip_weeks = {event for chip, event in chips if chip in ("wildcard", "freehit")}
    ft = 1
    for week in sorted(history, key=lambda w: w["event"]):
        if week["event"] < 2:
            continue
        used = 0 if week["event"] in chip_weeks else week["event_transfers"] - week["event_transfers_cost"] // 4
        ft = min(5, max(ft - used, 0) + 1)
    return ft


def load(entry_id, players):
    entry = data.get(f"entry/{entry_id}/")
    history = data.get(f"entry/{entry_id}/history/")
    chips = [(c["name"], c["event"]) for c in history["chips"]]
    weeks = history["current"]
    last = max(w["event"] for w in weeks)
    free_hit_weeks = {event for chip, event in chips if chip == "freehit"}
    squad_week = last - 1 if last in free_hit_weeks else last   # a Free Hit squad reverts afterwards
    picks = data.get(f"entry/{entry_id}/event/{squad_week}/picks/")
    squad = [p["element"] for p in picks["picks"]]
    bank = data.get(f"entry/{entry_id}/event/{last}/picks/")["entry_history"]["bank"] / 10

    bought = {}
    for t in sorted(data.get(f"entry/{entry_id}/transfers/"), key=lambda t: t["time"]):
        if t["event"] not in free_hit_weeks:
            bought[t["element_in"]] = t["element_in_cost"] / 10
    by_id = players.set_index("id")
    sell = {i: selling_price(bought.get(i, by_id.loc[i, "start_price"]), by_id.loc[i, "price"]) for i in squad}
    return Team(entry_id, entry["name"], squad, bank, free_transfers(weeks, chips), sell, chips)


def load_private(token, players):
    me = data.get("me/", token=token)["player"]
    entry = me["entry"]
    mine = data.get(f"my-team/{entry}/", token=token)
    public = data.get(f"entry/{entry}/history/")
    picks = sorted(mine["picks"], key=lambda p: p["position"])
    transfers = mine["transfers"]
    limit = transfers.get("limit")
    active = next((c.get("name") for c in mine.get("chips", []) if c.get("status_for_entry") == "active"), None)
    if active is None and any(p["position"] > 11 and p.get("multiplier", 0) > 0 for p in picks):
        active = "bboost"
    elif active is None and any(p.get("multiplier") == 3 for p in picks):
        active = "3xc"
    return Team(
        entry=entry, name=data.get(f"entry/{entry}/")["name"],
        squad=[p["element"] for p in picks], bank=transfers["bank"] / 10,
        free_transfers=0 if limit is None else min(max(limit - transfers.get("made", 0), 0), 5),
        sell_prices={p["element"]: p["selling_price"] / 10 for p in picks},
        chips_used=[(c["name"], c["event"]) for c in public["chips"]],
        picks=picks, transfers_open=limit is not None, active_chip=active)
