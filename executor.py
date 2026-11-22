"""Turning a plan into changes on FPL: the exact requests the website sends, sent and checked.

Hybrid policy:
  automatic   starting XI, bench order, captain and vice-captain; transfers that cost no points
              and add at least GAIN_PER_TRANSFER expected points each over waiting a week
  ask first   any transfer plan with a points hit, and any chip (approvals.py)
If transfers aren't made (waiting, or waiting for your OK), the lineup is picked for the squad
you actually have, not the one the plan would buy.

send() only sends when live (BOT_LIVE=true), never within 2 minutes of the deadline, stops at the
first refusal, and logs every request to state/writes.log. verify() reads my-team back afterwards.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

import data

GAIN_PER_TRANSFER = 1.0              # expected points over the horizon, vs. waiting a week, per transfer
ORDER = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
LOG = Path(__file__).with_name("state") / "writes.log"
LAST_SAFE = pd.Timedelta(minutes=2)  # no request this close to the deadline


@dataclass
class Action:
    kind: str            # "transfers" or "lineup"
    mode: str            # "auto" (does it), "approved" (you said yes), "ask" (needs your OK), "skip"
    why: str
    path: str            # API path it POSTs to
    payload: dict
    outcome: str = ""    # after send(): "sent", "shadow", or what went wrong
    expect: dict = field(default_factory=dict)


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
    if payload.get("chip"):
        changes.append(f"chip: {payload['chip']}")
    return changes


def lineup_action(team, week, players, chip=None, chip_already_set=False):
    """chip_already_set: the chip was played earlier this gameweek, so it's sent again with any
    change (as the website does) but isn't itself a reason to send."""
    payload = lineup_payload(week, players, chip)
    changes = lineup_changes(team.picks, dict(payload, chip=None) if chip_already_set else payload, players)
    if changes:
        return Action("lineup", "auto", "; ".join(changes), f"my-team/{team.entry}/", payload)
    return Action("lineup", "skip", "lineup and captain already right", f"my-team/{team.entry}/", payload)


def decide(team, planned, waiting, gain, players, per_transfer=GAIN_PER_TRANSFER, chip=None,
           chip_already_set=False):
    """The no-approval decision (plus an approved Triple Captain or Bench Boost, `chip`).
    planned: this week of the full plan. waiting: this week with no transfers. gain: planned vs. waiting."""
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
    lineup = lineup_action(team, lineup_week, players, chip, chip_already_set)
    if chip and not chip_already_set and lineup.mode == "auto":
        lineup.mode, lineup.why = "approved", f"{lineup.why} (you approved the chip)"
    actions.append(lineup)
    return actions


def _log(entry):
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")


def send(actions, token, deadline, live, job, now=None):
    """Send the auto/approved actions in order. Returns True if everything that should go, went.
    Stops at the first refusal: a lineup built for transfers that didn't happen would fail too."""
    start = now or pd.Timestamp.now(tz="UTC")
    began = pd.Timestamp.now(tz="UTC")
    for action in actions:
        if action.mode not in ("auto", "approved"):
            continue
        moment = start + (pd.Timestamp.now(tz="UTC") - began)
        entry = {"at": moment.isoformat(), "job": job, "kind": action.kind, "path": action.path,
                 "payload": action.payload}
        if not live:
            action.outcome = "shadow"
            _log(dict(entry, outcome="shadow (BOT_LIVE is not true)"))
            continue
        if moment > deadline - LAST_SAFE:
            action.outcome = "not sent: too close to the deadline"
            _log(dict(entry, outcome=action.outcome))
            return False
        try:
            response = data.post(action.path, token, action.payload)
            ok, detail = response.status_code in (200, 201, 202, 204), f"HTTP {response.status_code} {response.text[:300]}"
        except Exception as problem:            # network trouble: report it, don't guess
            ok, detail = False, f"{type(problem).__name__}: {problem}"
        action.outcome = "sent" if ok else f"FAILED: {detail}"
        _log(dict(entry, outcome=action.outcome))
        if not ok:
            return False
    return True


def verify(actions, mine):
    """Problems, if my-team (read back after sending) doesn't show what was sent."""
    problems = []
    squad = {p["element"] for p in mine["picks"]}
    for action in actions:
        if action.outcome != "sent":
            continue
        if action.kind == "transfers":
            missing = {t["element_in"] for t in action.payload["transfers"]} - squad
            kept = {t["element_out"] for t in action.payload["transfers"]} & squad
            if missing or kept:
                problems.append(f"transfers not showing on FPL (in missing {sorted(missing)}, out still there {sorted(kept)})")
        if action.kind == "lineup":
            now = {(p["element"], p["position"] <= 11, p["is_captain"], p["is_vice_captain"]) for p in mine["picks"]}
            sent = {(p["element"], p["position"] <= 11, p["is_captain"], p["is_vice_captain"])
                    for p in action.payload["picks"]}
            if now != sent:
                problems.append("lineup/captain on FPL doesn't match what was sent")
    return problems


def describe(actions, players, heading="WHAT THE BOT WOULD CHANGE ON FPL (dry run: nothing was sent)"):
    name = players.set_index("id")["web_name"]
    label = {"auto": "DO", "approved": "DO (you approved)", "ask": "ASK YOU FIRST", "skip": "LEAVE"}
    lines = [heading]
    for action in actions:
        result = f"  [{action.outcome}]" if action.outcome else ""
        lines.append(f"  {label[action.mode]} - {action.kind}: {action.why}{result}")
        if action.kind == "transfers":
            for t in action.payload["transfers"]:
                lines.append(f"      {name[t['element_out']]} (sell £{t['selling_price'] / 10:.1f}) -> "
                             f"{name[t['element_in']]} (buy £{t['purchase_price'] / 10:.1f})")
            if action.payload.get("chip"):
                lines.append(f"      with chip: {action.payload['chip']}")
    return "\n".join(lines)
