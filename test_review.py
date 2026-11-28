"""The gameweek review: FPL's scoring with automatic subs, the bot's lineups, when it runs, and the
whole review against a made-up gameweek.  python3 -m pytest test_review.py"""
import json

import pandas as pd
import pytest

import doc
import mailer
import review
from test_bot_flow import within_slack_limits

# A squad: 2 GK, 5 DEF, 5 MID, 3 FWD (ids 1-15), lined up 1-4-4-2 with the bench 12-15.
POSITIONS = {1: "GK", 2: "DEF", 3: "DEF", 4: "DEF", 5: "DEF", 6: "MID", 7: "MID", 8: "MID", 9: "MID",
             10: "FWD", 11: "FWD", 12: "GK", 13: "DEF", 14: "MID", 15: "FWD"}


def team(captain=10, vice=6, order=None):
    order = order or list(range(1, 16))
    return [{"element": e, "position": k + 1, "is_captain": e == captain, "is_vice_captain": e == vice}
            for k, e in enumerate(order)]


def played(points, out=()):
    """Everyone plays 90 minutes and scores `points` (a dict, default 2), except those in out."""
    return {e: (0, 0) if e in out else (90, points.get(e, 2)) for e in POSITIONS}


def test_scoring_matches_fpl():
    live = played({10: 10, 6: 5, 12: 6, 13: 3, 14: 1, 15: 4})
    # all play: the XI and the captain twice
    assert review.score(team(), live, POSITIONS)[0] == 9 * 2 + 10 + 5 + 10
    # the keeper doesn't play: only the reserve keeper can come on
    points, counted, subs, _ = review.score(team(), played({12: 6}, out={1}), POSITIONS)
    assert subs == [(1, 12)] and 12 in counted and points == 10 * 2 + 6 + 2
    # a defender doesn't play with 4 at the back: the first outfield sub in bench order comes on
    _, _, subs, _ = review.score(team(), played({}, out={2}), POSITIONS)
    assert subs == [(2, 13)]
    # 3 at the back and a defender misses: only a defender may replace him, so the DEF on the bench, not
    # the MID before him
    back3 = team(order=[1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 14, 12, 15, 5, 13])     # 1-3-5-2 (14 starts)
    _, _, subs, _ = review.score(back3, played({}, out={2}), POSITIONS)
    assert subs == [(2, 5)]
    # the captain doesn't play: the vice-captain's points count twice
    points, _, _, armband = review.score(team(), played({6: 5}, out={10}), POSITIONS)
    assert armband == 6 and points == 2 * 9 + 5 + 5 + 2      # 15 (FWD sub) comes on for 10
    # Bench Boost: all 15, no subs; Triple Captain: the captain three times
    assert review.score(team(), live, POSITIONS, "bboost")[0] == 9 * 2 + 10 + 5 + 10 + 6 + 3 + 1 + 4
    assert review.score(team(), live, POSITIONS, "3xc")[0] == 9 * 2 + 10 + 5 + 20


def test_gw12_lineup_comes_from_the_write_log(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "WRITES", tmp_path / "writes.log")
    monkeypatch.setattr(review, "LINEUPS", tmp_path / "lineups.json")
    events = pd.DataFrame({"id": [11, 12], "deadline_time": pd.to_datetime(["2026-11-21 11:00", "2026-11-27 18:30"], utc=True)})
    lines = [{"at": "2026-11-27T16:14:18+00:00", "job": "main", "kind": "lineup", "outcome": "sent",
              "payload": {"chip": None, "picks": team()}},
             {"at": "2026-11-27T17:43:58+00:00", "job": "final", "kind": "lineup", "outcome": "shadow (BOT_LIVE is not true)",
              "payload": {"chip": None, "picks": team(captain=11)}},
             {"at": "2026-11-20T16:00:00+00:00", "job": "main", "kind": "lineup", "outcome": "sent",
              "payload": {"chip": None, "picks": team(captain=2)}}]
    review.WRITES.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    found = review.lineup(12, events)
    assert found["picks"] == team() and found["source"] == "main run (write log)"   # sent, in GW12's window
    review.remember(12, team(captain=6), None, xp={"6": 5.0})
    assert review.lineup(12, events)["xp"] == {"6": 5.0}                       # kept lineups come first
    review.remember(12, team(captain=11), None, source="final check")          # a final check keeps the rest
    assert review.lineup(12, events)["xp"] == {"6": 5.0} and review.lineup(12, events)["source"] == "final check"


def test_a_review_is_due_once_fpl_confirms_the_points(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "REVIEWS", tmp_path / "reviews.json")
    events = pd.DataFrame({"id": [11, 12, 13], "data_checked": [True, True, False]})
    assert review.due(events) == 12
    assert review.due(events.assign(data_checked=[True, False, False])) is None     # GW11: before the bot
    review.mark(12, True, {"points": 50})
    assert review.due(events) is None
    for _ in range(review.MAX_TRIES):
        review.mark(13, False)
    assert review.due(events.assign(data_checked=True)) is None                  # gave up on GW13


class FakeGameweek:
    """GW13 as the public API would show it: you (entry 1) swapped the bot's captain, The Slash has
    three teams, one rival played Bench Boost and made a transfer."""
    def __init__(self):
        self.live = {**played({10: 2, 11: 12, 6: 7, 12: 1, 13: 6, 14: 3, 15: 5}, out={4}), 16: (90, 9)}
        self.mine = team(captain=11, vice=10)                                    # you: captain 11

    def get(self, path, **kwargs):
        elements = [{"id": e, "web_name": f"P{e}", "element_type": {"GK": 1, "DEF": 2, "MID": 3, "FWD": 4}[p]}
                    for e, p in {**POSITIONS, 16: "MID"}.items()]
        events = [{"id": g, "deadline_time": f"2026-11-{20 + g - 12}T18:30:00Z", "average_entry_score": 48,
                   "highest_score": 101, "data_checked": True} for g in (12, 13)]
        rival = {"picks": team(captain=6), "active_chip": "bboost", "entry_history": {"event_transfers_cost": 4}}
        return {
            "bootstrap-static/": {"elements": elements, "events": events},
            "event/13/live/": {"elements": [{"id": e, "stats": {"minutes": m, "total_points": p}}
                                            for e, (m, p) in self.live.items()]},
            "entry/1/event/13/picks/": {"picks": self.mine, "active_chip": None,
                                        "entry_history": {"points": review.score(self.mine, self.live, POSITIONS)[0],
                                                          "event_transfers_cost": 0, "overall_rank": 2_900_000}},
            "entry/1/history/": {"current": [{"event": 12, "overall_rank": 3_363_408, "total_points": 637},
                                             {"event": 13, "points": 53, "event_transfers_cost": 0, "total_points": 690}]},
            "entry/1/": {"leagues": {"classic": [{"id": 77, "name": "Overall"}, {"id": 99, "name": "The Slash "}]}},
            "leagues-classic/99/standings/": {"standings": {"has_next": False, "results": [
                {"entry": 3, "entry_name": "Third XI", "rank": 1, "last_rank": 1, "total": 999, "event_total": 2},
                {"entry": 2, "entry_name": "Rival FC", "rank": 2, "last_rank": 2, "total": 998, "event_total": 2},
                {"entry": 1, "entry_name": "FC Barcelona", "rank": 3, "last_rank": 3, "total": 997, "event_total": 0}]}},
            "entry/2/history/": {"current": [{"event": 12, "total_points": 650, "points": 60, "event_transfers_cost": 0},
                                             {"event": 13, "total_points": 716, "points": 70, "event_transfers_cost": 4}]},
            "entry/3/history/": {"current": [{"event": 12, "total_points": 640, "points": 60, "event_transfers_cost": 0},
                                             {"event": 13, "total_points": 680, "points": 40, "event_transfers_cost": 0}]},
            "entry/2/event/13/picks/": rival,
            "entry/3/event/13/picks/": {"picks": team(captain=10), "active_chip": None, "entry_history": {}},
            "entry/2/transfers/": [{"event": 13, "element_out": 9, "element_in": 16}, {"event": 12, "element_out": 1, "element_in": 2}],
            "entry/3/transfers/": [],
        }[path]


def test_the_review(tmp_path, monkeypatch):
    for name in ("LINEUPS", "REVIEWS", "WRITES"):
        monkeypatch.setattr(review, name, tmp_path / name)
    fake = FakeGameweek()
    monkeypatch.setattr(review.data, "get", fake.get)
    sent = []
    monkeypatch.setattr(mailer, "send", lambda subject, body, html=None, slack_posts=None, urgent=False:
                        sent.append((subject, body, slack_posts, urgent)) or True)
    xp = {str(e): 3.0 for e in POSITIONS}
    review.remember(13, team(), None, xp=xp, transfers=[[16, 11]], hits=0,
                    decisions=[{"n": 1, "kind": "chip", "chip": "bboost", "text": "Play Bench Boost", "answer": "no"}],
                    overrides={"keep": [], "ban": [16], "skip": [], "captain": None})
    review.mark(12, True, {"points": 50, "you": 50, "bot": 52, "projected": None, "league_rank": 3})
    assert review.run(13, 1)
    subject, body, posts, urgent = sent[-1]
    print(body)
    assert subject == "GW13 review: 53 points · 2nd in The Slash" and not urgent
    # 4 didn't play, so 13 (6) came on for both: 41 for the XI. You captained 11 (12), the bot 10 (2).
    assert "Your team: 53 points" in body and "The bot's lineup: 43 points" in body
    assert "Your changes: +10" in body and "Armband: yours P11 12 ×2; the bot's P10 2 ×2." in body
    assert "automatic subs: P4 → P13 6" in body
    assert "Projected 36.0, scored 37" in body and "Transfer P16 → P11: P11 scored 12" in body
    # after GW13 (from the teams' histories, not today's standings): Rival FC 716, you 690 (from 3rd), Third XI 680
    assert "You're 2nd of 3 (after GW12: 3rd). This week you scored 53; Rival FC had the most, 66." in body
    assert "Rival FC: Bench Boost" in body and "Rival FC: P9 → P16 (-4)" in body
    assert "Bench Boost: you said no; it would have added 9." in body     # 50 with all 15, 41 with the sub
    assert "You told the bot not to buy P16: he scored 9." in body
    assert "Your changes vs the bot's lineups: +8 over 2 gameweeks (1 better, 1 worse, 0 the same)." in body
    assert "The Slash: GW12 3rd, GW13 2nd." in body
    within_slack_limits(posts)
    assert review.due(pd.DataFrame({"id": [13], "data_checked": [True]})) is None       # done


def test_a_failed_review_is_retried_then_said_once(tmp_path, monkeypatch):
    for name in ("LINEUPS", "REVIEWS", "WRITES"):
        monkeypatch.setattr(review, name, tmp_path / name)
    monkeypatch.setattr(review.data, "get", lambda path, **k: (_ for _ in ()).throw(ConnectionError("503")))
    sent = []
    monkeypatch.setattr(mailer, "send", lambda subject, body, *a, **k: sent.append(subject) or True)
    for _ in range(review.MAX_TRIES - 1):
        assert not review.run(13, 1)
    assert sent == []
    assert not review.run(13, 1)
    assert sent == ["GW13 review: couldn't be made"]
