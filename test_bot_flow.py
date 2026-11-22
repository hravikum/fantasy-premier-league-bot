"""The gameweek jobs end to end against a fake FPL (real archive data, a fake server that applies
the bot's requests the way FPL would and serves my-team back).  python3 -m pytest test_bot_flow.py"""
from collections import Counter

import pandas as pd
import pytest

import approvals
import auth
import bot
import data
import executor
import mailer
import news
import optimizer
import schedule

SEASON, GW = "2026-27", 7


class FakeFPL:
    def __init__(self, refuse=()):
        self.players = data._archive_csv(SEASON, "players_raw.csv")
        self.fixtures = data._archive_csv(SEASON, "fixtures.csv")
        gw = data._archive_csv(SEASON, "gws/merged_gw.csv")
        self.summaries = {k: g.drop(columns=["name", "position", "team", "xP"]).to_dict("records")
                          for k, g in gw.groupby("element")}
        kickoffs = pd.to_datetime(self.fixtures.kickoff_time, utc=True).groupby(self.fixtures.event).min()
        self.deadlines = kickoffs - pd.Timedelta("90min")
        self.picks = self.legal_squad()
        self.bank, self.posts, self.refuse = 15, [], set(refuse)

    def legal_squad(self):
        squad, clubs = [], Counter()
        quota = {1: 2, 2: 5, 3: 5, 4: 3}
        for row in self.players.sort_values("total_points", ascending=False).itertuples():
            if quota[row.element_type] and clubs[row.team] < 3 and row.status == "a":
                squad.append((row.element_type, row.id))
                quota[row.element_type] -= 1
                clubs[row.team] += 1
        squad.sort()
        order = [squad[0]] + [s for s in squad if s[0] == 2][:4] + [s for s in squad if s[0] == 3][:4] + \
                [s for s in squad if s[0] == 4][:2] + [squad[1]] + [s for s in squad if s[0] == 2][4:] + \
                [s for s in squad if s[0] == 3][4:] + [s for s in squad if s[0] == 4][2:]
        picks = [{"element": e, "position": k + 1, "is_captain": k == 9, "is_vice_captain": k == 8,
                  "selling_price": int(self.players.set_index("id").loc[e, "now_cost"]), "purchase_price": 0}
                 for k, (_, e) in enumerate(order)]
        return self.multipliers(picks, None)

    @staticmethod
    def multipliers(picks, chip):
        """As FPL shows them: captain 2 (3 with Triple Captain), XI 1, bench 0 (1 with Bench Boost)."""
        for p in picks:
            p["multiplier"] = ((3 if chip == "3xc" else 2) if p["is_captain"]
                               else 1 if p["position"] <= 11 or chip == "bboost" else 0)
        return picks

    def get(self, path, session=None, token=None, **kwargs):
        if path.startswith("element-summary/"):
            return {"history": self.summaries.get(int(path.split("/")[1]), [])}
        events = [{"id": int(e), "deadline_time": t.isoformat(), "finished": int(e) < GW} for e, t in self.deadlines.items()]
        return {
            "bootstrap-static/": {"events": events, "teams": data._archive_csv(SEASON, "teams.csv").to_dict("records"),
                                  "elements": self.players.to_dict("records")},
            "fixtures/": self.fixtures.to_dict("records"),
            "me/": {"player": {"entry": 99}},
            "entry/99/": {"name": "Test FC"},
            "entry/99/history/": {"current": [], "chips": [{"name": "wildcard", "event": 2}, {"name": "freehit", "event": 4}]},
            "my-team/99/": {"picks": [dict(p) for p in self.picks], "chips": [],
                            "transfers": {"limit": 2, "made": 0, "bank": self.bank, "value": 1000}},
        }[path]

    def post(self, path, token, payload):
        self.posts.append((path, payload))
        status = 400 if any(path.startswith(r) for r in self.refuse) else 200
        if status == 200 and path == "transfers/":
            for t in payload["transfers"]:
                slot = next(p for p in self.picks if p["element"] == t["element_out"])
                slot.update(element=t["element_in"], selling_price=t["purchase_price"])
                self.bank += t["selling_price"] - t["purchase_price"]
        elif status == 200 and path.startswith("my-team/"):
            self.picks = self.multipliers([dict(p, selling_price=next(q["selling_price"] for q in self.picks
                                                                       if q["element"] == p["element"]))
                                           for p in payload["picks"]], payload["chip"])
        return type("Response", (), {"status_code": status, "text": "{}" if status == 200 else '{"detail":"no"}'})()


@pytest.fixture
def fpl(tmp_path, monkeypatch):
    def setup(refuse=(), reply=None):
        server = FakeFPL(refuse)
        monkeypatch.setattr(optimizer, "POOL", {"GK": 5, "DEF": 15, "MID": 15, "FWD": 10})   # same plans, faster
        monkeypatch.setattr(data, "get", server.get)
        monkeypatch.setattr(data, "post", server.post)
        monkeypatch.setattr(auth, "access_token", lambda: "token")
        monkeypatch.setattr(approvals, "check", lambda code, since: reply)
        server.emails = []
        monkeypatch.setattr(mailer, "send", lambda subject, body, html=None:
                            server.emails.append((subject, body, html)) or True)
        for module, name in [(schedule, "STATE"), (approvals, "STATE"), (executor, "LOG"), (bot, "FLAGS"),
                             (news, "WATCHLIST"), (news, "NEWS"), (news, "HEARTBEAT")]:
            monkeypatch.setattr(module, name, tmp_path / f"{module.__name__}-{name}")
        return server
    return setup


def test_preview_main_final(fpl):
    server = fpl()
    deadline = server.deadlines[GW]
    assert bot.tick(deadline - pd.Timedelta(hours=24), live=True) == "preview"
    assert server.posts == [] and "preview" in server.emails[-1][0]
    assert "bar is" not in server.emails[-1][1]          # never plans transfers it would then refuse to make
    assert "<table" in server.emails[-1][2]               # sent as HTML too
    assert news.WATCHLIST.exists()

    assert bot.tick(deadline - pd.Timedelta(hours=2), live=True) == "main"
    paths = [p for p, _ in server.posts]
    assert paths and paths[-1] == "my-team/99/" and set(paths) <= {"transfers/", "my-team/99/"}
    assert "main run done" in server.emails[-1][0]
    assert executor.LOG.read_text().count('"outcome":"sent"') == len(paths)
    assert bot.FLAGS.exists()

    sent = len(server.posts)
    assert bot.tick(deadline - pd.Timedelta(minutes=45), live=True) == "final"
    assert len(server.posts) == sent                     # lineup already right: nothing sent, no email
    assert bot.tick(deadline - pd.Timedelta(minutes=20), live=True) is None
    assert set(schedule.load(GW, deadline)["done"]) == {"preview", "main", "final"}


def test_shadow_sends_nothing(fpl):
    server = fpl()
    deadline = server.deadlines[GW]
    assert bot.tick(deadline - pd.Timedelta(hours=2), live=False) == "main"
    assert server.posts == [] and server.emails[-1][0].startswith("[shadow]")


def test_refusal_emails_action_needed_and_final_retries(fpl):
    server = fpl(refuse=("my-team/",))
    deadline = server.deadlines[GW]
    assert bot.tick(deadline - pd.Timedelta(hours=2), live=True) == "main"
    assert server.emails[-1][0].startswith("FPL ACTION NEEDED") and "HTTP 400" in server.emails[-1][1]
    assert "main" not in schedule.load(GW, deadline)["done"]
    assert bot.tick(deadline - pd.Timedelta(hours=1.5), live=True) == "main"     # next tick tries again
    server.refuse.clear()
    assert bot.tick(deadline - pd.Timedelta(minutes=45), live=True) == "final"   # final runs the main run
    assert "final run done" in server.emails[-1][0]
    assert {"main", "final"} <= set(schedule.load(GW, deadline)["done"])


def test_approved_bench_boost_is_played(fpl):
    server = fpl(reply="yes")
    deadline = server.deadlines[GW]
    approvals.save({"gw": GW, "code": "fpl-000000", "created": (deadline - pd.Timedelta(hours=24)).isoformat(),
                    "chip": "bboost", "transfers": None, "asks": ["Play Bench Boost"]})
    assert bot.tick(deadline - pd.Timedelta(hours=2), live=True) == "main"
    lineup = [payload for path, payload in server.posts if path == "my-team/99/"][-1]
    assert lineup["chip"] == "bboost"
    assert schedule.load(GW, deadline)["chip"] == "bboost"


def test_a_chip_you_played_yourself_is_kept(fpl):
    server = fpl()
    server.multipliers(server.picks, "bboost")  # you played Bench Boost on the site
    deadline = server.deadlines[GW]
    assert bot.tick(deadline - pd.Timedelta(hours=2), live=True) == "main"
    lineups = [payload for path, payload in server.posts if path == "my-team/99/"]
    assert all(l["chip"] == "bboost" for l in lineups)
    assert "already played Bench Boost" in server.emails[-1][1]


def test_check_write_changes_nothing(fpl):
    server = fpl()
    before = [dict(p) for p in server.picks]
    ctx = bot.load_context(server.deadlines[GW] - pd.Timedelta(hours=30), live=False)
    assert bot.check_write(ctx)
    key = lambda picks: sorted((p["element"], p["position"], p["is_captain"]) for p in picks)
    assert key(server.picks) == key(before) and len(server.posts) == 1
