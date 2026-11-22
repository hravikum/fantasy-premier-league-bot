"""What the bot would send to FPL, and whether it would do it on its own.

Phase 3 is a dry run: the exact requests the FPL site sends are built and shown, nothing is sent.

Hybrid policy:
  automatic   starting XI, bench order, captain and vice-captain; transfers that cost no points
              and beat waiting a week by at least GAIN_PER_TRANSFER expected points per transfer
  ask first   any transfer plan with a points hit, and any chip
If transfers aren't made (waiting, or waiting for your OK), the lineup is picked for the squad
you actually have, not the one the plan would buy.
"""
import json
from dataclasses import dataclass

GAIN_PER_TRANSFER = 1.0              # expected points over the horizon, vs. waiting a week, per transfer
ORDER = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}


@dataclass
class Action:
    kind: str            # "transfers" or "lineup"
    mode: str            # "auto" (would do it), "ask" (would email for your OK), "skip"
    why: str
    path: str            # API path it would POST to
    payload: dict


def transfers_payload(entry, week, players, sell_prices, chip=None):
    price = players.set_index("id")["price"]
    return {"confirmed": True, "entry": int(entry), "event": int(week.gw), "chip": chip, "transfers": [
        {"element_in": int(i), "element_out": int(o),
         "purchase_price": int(round(price[i] * 10)), "selling_price": int(round(sell_prices[o] * 10))}
        for o, i in zip(week.transfers_out, week.transfers_in)]}


def lineup_payload(week, players, chip=None):
    """Positions 1-11 are the XI (keeper first, then by position), 12 the reserve keeper, 13-15 the bench."""
    pos = players.set_index("id")["position"]
    order = sorted(week.xi, key=lambda i: ORDER[pos[i]]) + list(week.bench)
    return {"chip": chip, "picks": [
        {"element": int(e), "position": k + 1, "is_captain": e == week.captain, "is_vice_captain": e == week.vice_captain}
        for k, e in enumerate(order)]}


def lineup_changes(current, payload, players):
    """Plain-English differences between the lineup on FPL now and the proposed one."""
    if not current:
        return ["(current lineup unknown: not logged in)"]
    name = players.set_index("id")["web_name"]
    now_xi = {p["element"] for p in current if p["position"] <= 11}
    new_xi = {p["element"] for p in payload["picks"] if p["position"] <= 11}
    role = lambda picks, key: next((p["element"] for p in picks if p[key]), None)
    changes = []
    if new_xi - now_xi:
        changes.append("starts: " + ", ".join(name[i] for i in sorted(new_xi - now_xi)))
    if now_xi - new_xi:
        changes.append("benched/out: " + ", ".join(name[i] for i in sorted(now_xi - new_xi)))
    for key, label in (("is_captain", "captain"), ("is_vice_captain", "vice-captain")):
        before, after = role(current, key), role(payload["picks"], key)
        if before != after:
            changes.append(f"{label}: {name.get(before, '?')} -> {name[after]}")
    bench_now = [p["element"] for p in sorted(current, key=lambda p: p["position"]) if p["position"] > 11]
    bench_new = [p["element"] for p in payload["picks"] if p["position"] > 11]
    if bench_now != bench_new and set(bench_now) == set(bench_new):
        changes.append("bench order: " + ", ".join(name[i] for i in bench_new))
    return changes


def decide(team, planned, waiting, gain, players, per_transfer=GAIN_PER_TRANSFER):
    """planned: this week of the full plan. waiting: this week with no transfers. gain: planned vs. waiting."""
    actions, lineup_week = [], waiting
    if planned.transfers_in:
        payload = transfers_payload(team.entry, planned, players, team.sell_prices)
        if not team.transfers_open:
            mode, why = "skip", "a Wildcard or Free Hit is active this week: transfers are left to you"
        elif planned.hits:
            mode, why = "ask", f"{planned.hits} hit(s), -{4 * planned.hits}: needs your OK"
        elif gain < per_transfer * len(planned.transfers_in):      # plan_this_week normally sees to this
            bar = per_transfer * len(planned.transfers_in)
            mode, why = "skip", f"only {gain:+.1f} xP better than waiting a week (bar is {bar:+.1f})"
        else:
            mode, why = "auto", f"free transfer(s), {gain:+.1f} xP better than waiting a week"
            lineup_week = planned
        actions.append(Action("transfers", mode, why, "transfers/", payload))
    payload = lineup_payload(lineup_week, players)
    changes = lineup_changes(team.picks, payload, players)
    if changes:
        actions.append(Action("lineup", "auto", "; ".join(changes), f"my-team/{team.entry}/", payload))
    else:
        actions.append(Action("lineup", "skip", "lineup and captain already right", f"my-team/{team.entry}/", payload))
    return actions


def describe(actions, players):
    name = players.set_index("id")["web_name"]
    label = {"auto": "WOULD DO", "ask": "WOULD ASK YOU FIRST", "skip": "WOULD LEAVE"}
    lines = ["WHAT THE BOT WOULD CHANGE ON FPL (dry run: nothing was sent)"]
    for action in actions:
        lines.append(f"  {label[action.mode]} - {action.kind}: {action.why}")
        if action.kind == "transfers":
            for t in action.payload["transfers"]:
                lines.append(f"      {name[t['element_out']]} (sell £{t['selling_price'] / 10:.1f}) -> "
                             f"{name[t['element_in']]} (buy £{t['purchase_price'] / 10:.1f})")
        if action.mode != "skip":
            lines.append(f"      POST /api/{action.path} {json.dumps(action.payload, separators=(',', ':'))}")
    return "\n".join(lines)
