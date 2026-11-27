"""The gameweek jobs end to end against a fake FPL (real archive data, a fake server that applies
the bot's requests the way FPL would and serves my-team back).  python3 -m pytest test_bot_flow.py"""
from collections import Counter

import pandas as pd
import pytest

import approvals
import overrides
import auth
import bot
import data
import executor
import mailer
import news
import optimizer
import schedule
import slack

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
        monkeypatch.setattr(approvals, "check", lambda code, since, count, times=None: (reply or {}, []))
        server.emails, server.slack = [], []
        monkeypatch.setattr(mailer, "send", lambda subject, body, html=None, slack_posts=None, urgent=False:
                            server.emails.append((subject, body, html)) or server.slack.append((slack_posts, urgent))
                            or True)
        for module, name in [(schedule, "STATE"), (approvals, "STATE"), (executor, "LOG"), (bot, "FLAGS"),
                             (news, "WATCHLIST"), (news, "NEWS"), (news, "HEARTBEAT")]:
            monkeypatch.setattr(module, name, tmp_path / f"{module.__name__}-{name}")
        return server
    return setup


def within_slack_limits(posts):
    """Each post fits what Slack takes: 50 blocks, one table of 100 rows, 3,000 characters a section."""
    for post in posts:
        for layout in post["layouts"]:
            assert 0 < len(layout) <= slack.MAX_BLOCKS
            assert sum(b["type"] == "table" for b in layout) <= 1
            for b in layout:
                if b["type"] == "section":
                    assert len(b["text"]["text"]) <= slack.SECTION
                elif b["type"] == "header":
                    assert len(b["text"]["text"]) <= slack.HEADER
                elif b["type"] == "context":
                    assert all(len(e["text"]) <= slack.SECTION for e in b["elements"])
                elif b["type"] == "table":
                    assert len(b["rows"]) <= slack.MAX_ROWS and all(len(row) <= 20 for row in b["rows"])
                    assert sum(len(cell["text"]) for row in b["rows"] for cell in row) <= 10000
    return posts


def test_preview_main_final(fpl):
    server = fpl()
    deadline = server.deadlines[GW]
    assert bot.tick(deadline - pd.Timedelta(hours=24), live=True) == "preview"
    assert server.posts == [] and "preview" in server.emails[-1][0]
    assert "bar is" not in server.emails[-1][1]          # never plans transfers it would then refuse to make
    assert "<table" in server.emails[-1][2]               # sent as HTML too
    posts, urgent = server.slack[-1]
    assert not urgent and len(within_slack_limits(posts)) > 1    # the report's detail goes in the thread
    assert news.WATCHLIST.exists()

    assert bot.tick(deadline - pd.Timedelta(hours=2), live=True) == "main"
    paths = [p for p, _ in server.posts]
    assert paths and paths[-1] == "my-team/99/" and set(paths) <= {"transfers/", "my-team/99/"}
    assert "main run done" in server.emails[-1][0]
    within_slack_limits(server.slack[-1][0])
    assert executor.LOG.read_text().count('"outcome":"sent"') == len(paths)
    assert bot.FLAGS.exists()

    sent = len(server.posts)
    assert bot.tick(deadline - pd.Timedelta(minutes=45), live=True) == "final"
    assert len(server.posts) == sent                     # lineup already right: nothing sent, no email
    assert bot.tick(deadline - pd.Timedelta(minutes=20), live=True) is None
    assert set(schedule.load(GW, deadline)["done"]) == {"preview", "main", "final"}


def change_captain(server):
    """You, on the FPL site: the vice-captain becomes captain and the captain vice-captain."""
    for p in server.picks:
        p["is_captain"], p["is_vice_captain"] = p["is_vice_captain"], p["is_captain"]


def test_your_changes_after_the_main_run_are_left_alone(fpl):
    server = fpl()
    deadline = server.deadlines[GW]
    assert bot.tick(deadline - pd.Timedelta(hours=2), live=True) == "main"
    change_captain(server)
    mine, sent, emails = [dict(p) for p in server.picks], len(server.posts), len(server.emails)
    assert bot.tick(deadline - pd.Timedelta(minutes=45), live=True) == "final"
    assert len(server.posts) == sent and server.picks == mine and len(server.emails) == emails


def test_a_starter_flagged_after_your_changes_is_flagged_to_you(fpl):
    server = fpl()
    deadline = server.deadlines[GW]
    assert bot.tick(deadline - pd.Timedelta(hours=2), live=True) == "main"
    change_captain(server)
    starter = next(p["element"] for p in server.picks if p["position"] == 1)
    row = server.players.id == starter
    server.players.loc[row, ["status", "chance_of_playing_next_round", "news"]] = ["i", 0, "Hamstring injury"]
    sent = len(server.posts)
    assert bot.tick(deadline - pd.Timedelta(minutes=45), live=True) == "final"
    assert len(server.posts) == sent                     # still yours: nothing sent
    subject, body, _ = server.emails[-1]
    assert subject.startswith("ACTION NEEDED") and "Hamstring injury (100% → 0% chance of playing)" in body
    assert server.slack[-1][1]                           # urgent: Slack with an @-mention, and email


def test_without_your_changes_the_final_check_still_fixes_the_lineup(fpl):
    server = fpl()
    deadline = server.deadlines[GW]
    assert bot.tick(deadline - pd.Timedelta(hours=2), live=True) == "main"
    starter = next(p["element"] for p in server.picks if p["position"] == 1)
    row = server.players.id == starter
    server.players.loc[row, ["status", "chance_of_playing_next_round", "news"]] = ["i", 0, "Hamstring injury"]
    sent = len(server.posts)
    assert bot.tick(deadline - pd.Timedelta(minutes=45), live=True) == "final"
    assert len(server.posts) == sent + 1 and "lineup updated" in server.emails[-1][0]
    assert starter not in [p["element"] for p in server.picks if p["position"] <= 11]


def test_shadow_sends_nothing(fpl):
    server = fpl()
    deadline = server.deadlines[GW]
    assert bot.tick(deadline - pd.Timedelta(hours=2), live=False) == "main"
    assert server.posts == [] and server.emails[-1][0].startswith("[shadow]")


def test_refusal_emails_action_needed_and_final_retries(fpl):
    server = fpl(refuse=("my-team/",))
    deadline = server.deadlines[GW]
    assert bot.tick(deadline - pd.Timedelta(hours=2), live=True) == "main"
    assert server.emails[-1][0].startswith("ACTION NEEDED") and "HTTP 400" in server.emails[-1][1]
    assert server.slack[-1][1]                           # urgent: email and Slack, with an @-mention
    assert "main" not in schedule.load(GW, deadline)["done"]
    assert bot.tick(deadline - pd.Timedelta(hours=1.5), live=True) == "main"     # next tick tries again
    server.refuse.clear()
    assert bot.tick(deadline - pd.Timedelta(minutes=45), live=True) == "final"   # final runs the main run
    assert "final run done" in server.emails[-1][0]
    assert {"main", "final"} <= set(schedule.load(GW, deadline)["done"])


def test_no_to_the_hit_yes_to_the_chip(fpl):
    server = fpl(reply={1: "no", 2: "yes"})
    deadline = server.deadlines[GW]
    squad = [p["element"] for p in server.picks]
    outsider = next(int(i) for i in server.players.sort_values("total_points", ascending=False).id if i not in squad)
    approvals.save({"gw": GW, "code": "fpl-000000", "created": (deadline - pd.Timedelta(hours=24)).isoformat(),
                    "decisions": [
                        {"n": 1, "kind": "transfers", "in": [outsider], "out": [squad[5]], "hits": 1, "text": "Take 1 hit"},
                        {"n": 2, "kind": "chip", "chip": "bboost", "text": "Play Bench Boost"}]})
    assert bot.tick(deadline - pd.Timedelta(hours=2), live=True) == "main"
    lineup = [payload for path, payload in server.posts if path == "my-team/99/"][-1]
    assert lineup["chip"] == "bboost"
    assert all(outsider not in [t["element_in"] for t in payload["transfers"]]
               for path, payload in server.posts if path == "transfers/")
    assert schedule.load(GW, deadline)["chip"] == "bboost"
    body = server.emails[-1][1]
    assert "1. Take 1 hit: you said no" in body and "2. Play Bench Boost: you said yes" in body


def test_a_tap_in_slack_counts_and_the_main_run_closes_the_buttons(fpl, monkeypatch):
    server = fpl(reply={1: "no", 2: "no"})                # by email: no to both (no times: older than any tap)
    deadline = server.deadlines[GW]
    squad = [p["element"] for p in server.picks]
    outsider = next(int(i) for i in server.players.sort_values("total_points", ascending=False).id if i not in squad)
    approvals.save({"gw": GW, "code": "fpl-000000", "created": (deadline - pd.Timedelta(hours=24)).isoformat(),
                    "slack_ts": "1.2", "decisions": [
                        {"n": 1, "kind": "transfers", "in": [outsider], "out": [squad[5]], "hits": 1, "text": "Take 1 hit"},
                        {"n": 2, "kind": "chip", "chip": "bboost", "text": "Play Bench Boost"}]})
    message = {"text": "GW7 preview", "blocks": [{"type": "context", "block_id": "answer-2-yes-1764260000"}]}
    closed = []
    monkeypatch.setattr(slack, "configured", lambda: True)
    monkeypatch.setattr(approvals, "slack_message", lambda ts: message)
    monkeypatch.setattr(approvals, "close_slack", lambda proposal, found, note: closed.append((proposal["slack_ts"], note)))
    assert bot.tick(deadline - pd.Timedelta(hours=2), live=True) == "main"
    lineup = [payload for path, payload in server.posts if path == "my-team/99/"][-1]
    assert lineup["chip"] == "bboost"                      # your tap in Slack
    body = server.emails[-1][1]
    assert "1. Take 1 hit: you said no" in body and "2. Play Bench Boost: you said yes" in body
    assert closed and closed[0][0] == "1.2" and closed[0][1].startswith("🔒 Answers closed at the main run")


def test_your_overrides_steer_the_main_run_and_the_final_check_keeps_your_captain(fpl, monkeypatch):
    server = fpl()
    deadline = server.deadlines[GW]
    squad = [p["element"] for p in server.picks]
    fit = server.players[server.players.id.isin(squad) & server.players.status.eq("a")]
    pick = int(fit[fit.element_type > 1].sort_values("total_points").id.iloc[0])     # the bot wouldn't captain him
    best_outsider = next(int(i) for i in server.players.sort_values("total_points", ascending=False).id if i not in squad)
    monkeypatch.setattr(overrides, "read", lambda gw: overrides.Overrides(captain=pick, ban={best_outsider}))
    assert bot.tick(deadline - pd.Timedelta(hours=2), live=True) == "main"
    lineup = [payload for path, payload in server.posts if path == "my-team/99/"][-1]
    assert next(p for p in lineup["picks"] if p["is_captain"])["element"] == pick
    bought = [t["element_in"] for path, payload in server.posts if path == "transfers/" for t in payload["transfers"]]
    assert best_outsider not in bought
    name = server.players.set_index("id").loc[pick, "web_name"]
    assert f"Your overrides: don't buy" in server.emails[-1][1] and f"captain {name}" in server.emails[-1][1]
    sent = len(server.posts)
    assert bot.tick(deadline - pd.Timedelta(minutes=45), live=True) == "final"
    assert len(server.posts) == sent                     # your captain kept: nothing to fix


def test_the_preview_puts_the_override_controls_in_slack_only(fpl):
    server = fpl()
    assert bot.tick(server.deadlines[GW] - pd.Timedelta(hours=24), live=True) == "preview"
    channel = server.slack[-1][0][0]["layouts"][0]
    menus = [e for b in channel if b["type"] == "actions" for e in b["elements"] if e["type"] == "static_select"]
    code = approvals.load(GW)["code"]                     # a code even without decisions
    assert menus and all(o["value"].startswith(f"{code}|captain.gw{GW}|") for o in menus[0]["options"])
    assert "Change the bot's moves" not in server.emails[-1][1] and "Change the bot" not in server.emails[-1][2]


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


def test_suspended_player_goes_with_a_free_transfer_and_fixtures_show(fpl):
    """A starting defender suspended for this gameweek and the next: the preview sells him under the
    rule-out rule (or says why he stays), the fixtures table marks him out, and the main run does it."""
    server = fpl()
    deadline = server.deadlines[GW]
    elements = server.players.set_index("id")
    defenders = [p["element"] for p in server.picks if elements.loc[p["element"], "element_type"] == 2]
    hurt = min(defenders, key=lambda e: elements.loc[e, "total_points"])
    back = (server.deadlines[GW + 2] + pd.Timedelta(days=1)).strftime("%d %b")
    row = server.players.id == hurt
    server.players.loc[row, ["status", "chance_of_playing_next_round", "news"]] = ["s", 0, f"Suspended until {back}"]
    name = elements.loc[hurt, "web_name"]

    assert bot.tick(deadline - pd.Timedelta(hours=24), live=True) == "preview"
    subject, text, html = server.emails[-1]
    print(text)
    assert f"FIXTURES, GW{GW}–{GW + 2}" in text and f"{name}(OUT)" in text
    assert f"GW{GW}–{GW + 1}, so he goes now" in text or f"{name} goes now" in text
    assert "Badge" not in html and ">OUT<" in html
    posts = within_slack_limits(server.slack[-1][0])
    lines = [line for post in posts for b in post["layouts"][0]
             if b["type"] == "section" for line in b["text"]["text"].split("\n")]
    assert any(line.startswith(f"🔁 *{name}*") for line in lines)                          # transfers
    fixtures = next(row for post in posts for b in post["layouts"][0] if b["type"] == "table"
                    for row in b["rows"] if row[0]["text"] == f"{name} (OUT)")
    assert fixtures[2]["text"].endswith(" out")                                             # ruled out that week

    assert bot.tick(deadline - pd.Timedelta(hours=2), live=True) == "main"
    sent = [p for path, p in server.posts if path == "transfers/"]
    assert sent and hurt in [t["element_out"] for t in sent[0]["transfers"]]
    assert "ruled out" in server.emails[-1][1]


def test_squad_check_runs(fpl, monkeypatch, capsys):
    import explain
    import team as team_module
    server = fpl()
    def public(entry, players):
        squad = [p["element"] for p in server.picks]
        by_id = players.set_index("id")
        return team_module.Team(entry, "Test FC", squad, 1.5, 2, {i: by_id.loc[i, "price"] for i in squad})
    monkeypatch.setattr(team_module, "load", public)
    monkeypatch.setattr(pd.Timestamp, "now", staticmethod(lambda tz=None: server.deadlines[GW] - pd.Timedelta(hours=24)))
    report = explain.squad_check(99)
    print(report)
    for part in ["1. OWNERSHIP", "2. HOW OUR PROJECTIONS COMPARE", "3. CLUB EXPOSURE", "4. DEFENCES", "5. WHAT LIMITS"]:
        assert part in report
