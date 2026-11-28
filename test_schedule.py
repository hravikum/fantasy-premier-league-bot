"""Schedule, approvals, chips and news tests.  python3 -m pytest test_schedule.py"""
import json
from email.message import EmailMessage

import pandas as pd

import approvals
import chips
import news
import schedule

DEADLINE = pd.Timestamp("2026-11-27 18:30", tz="UTC")
H = pd.Timedelta(hours=1)


def at(hours_before):
    return DEADLINE - hours_before * H


def test_each_job_runs_once_in_its_window(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "STATE", tmp_path / "schedule.json")
    record = schedule.load(12, DEADLINE)
    assert schedule.due(record, DEADLINE, at(30)) is None
    assert schedule.due(record, DEADLINE, at(24)) == "preview"
    schedule.mark(record, "preview", at(24), ok=True)
    assert schedule.due(record, DEADLINE, at(20)) is None
    assert schedule.due(record, DEADLINE, at(2.2)) == "main"
    schedule.mark(record, "main", at(2.2), ok=False)            # failed: tried again next tick
    assert schedule.due(record, DEADLINE, at(1.7)) == "main" and record["failures"]["main"] == 1
    schedule.mark(record, "main", at(1.7), ok=True)
    assert schedule.due(record, DEADLINE, at(0.75)) == "final"
    schedule.mark(record, "final", at(0.75), ok=True)
    assert schedule.due(record, DEADLINE, at(0.3)) is None and schedule.due(record, DEADLINE, at(0.05)) is None
    schedule.save(record)
    assert schedule.load(12, DEADLINE)["done"].keys() == {"preview", "main", "final"}
    assert schedule.load(13, DEADLINE + 96 * H)["done"] == {}      # a new gameweek starts fresh


def test_thirty_minute_ticks_never_miss_a_window():
    for offset in range(0, 30):
        ticks = [DEADLINE - 30 * H + pd.Timedelta(minutes=offset + 30 * k) for k in range(60)]
        hit = {job for t in ticks for job, (o, c) in schedule.WINDOWS.items() if c <= DEADLINE - t < o}
        assert hit == set(schedule.WINDOWS)


def reply(subject, body, sender="me@gmail.com"):
    message = EmailMessage()
    message["Subject"], message["From"] = subject, sender
    message.set_content(body)
    return message


CODE = "fpl-abc123"
OWNERS = {"me@gmail.com"}
QUOTED = "\n\nOn Thu, 26 Nov 2026 at 13:43, me@gmail.com wrote:\n> 1. Take 1 hit ... 2. Play Bench Boost"
RE = f"Re: FPL: GW12 preview, deadline Fri 27 Nov 13:30: 2 decisions for you [{CODE}]"


def answers(*messages, count=2):
    return approvals.verdicts(list(messages), CODE, OWNERS, count)


def test_each_decision_gets_its_own_answer():
    assert answers(reply(RE, "1 yes\n2 no" + QUOTED))[0] == {1: "yes", 2: "no"}
    assert answers(reply(RE, "1: no, 2: yes please" + QUOTED))[0] == {1: "no", 2: "yes"}
    assert answers(reply(RE, "#2 ok" + QUOTED))[0] == {2: "yes"}                      # 1 unanswered: skipped
    assert answers(reply(RE, "No to 1, yes to 2" + QUOTED))[0] == {1: "no", 2: "yes"}
    assert answers(reply(RE, "yes, no" + QUOTED))[0] == {1: "yes", 2: "no"}           # in the order listed
    assert answers(reply(RE, "Yes" + QUOTED), count=1)[0] == {1: "yes"}               # one decision: plain yes


def test_the_buttons():
    button = lambda n, a: reply(f"FPL decision {CODE} #{n} {a.upper()}", f"{n} {a}")
    assert answers(button(1, "yes"), button(2, "no"))[0] == {1: "yes", 2: "no"}
    link = approvals.button_link(CODE, 2, "no")
    assert link.startswith("mailto:") and "%232%20NO" in link and CODE in link


def test_a_bare_yes_with_two_decisions_approves_nothing():
    found, notes = answers(reply(RE, "yes" + QUOTED))
    assert found == {} and "didn't say which decision" in notes[0]


def test_later_answers_win_and_strangers_dont_count():
    assert answers(reply(RE, "1 yes 2 yes"), reply(RE, "2 no, changed my mind"))[0] == {1: "yes", 2: "no"}
    assert answers(reply(RE, "1 yes", sender="x@evil.com"))[0] == {}
    assert answers(reply(RE.replace(CODE, "fpl-zzz999"), "1 yes"))[0] == {}
    # the bot's own email (it contains the code and words like "yes") is never an answer
    assert answers(reply(f"FPL: GW12 preview: 2 decisions for you [{CODE}]", "1 yes 2 yes"))[0] == {}
    # only the quoted preview, nothing typed: no answer
    assert answers(reply(RE, "> 1 yes\n> 2 no"))[0] == {}
    # Gmail sometimes splits the "On ... wrote:" line in two
    assert answers(reply(RE, "2 yes\n\nOn Thu, Nov 26, 2026 at 1:43 PM Hari <\nme@gmail.com> wrote:\n1 yes"))[0] == {2: "yes"}


def test_chips_spread_one_a_week_and_wait_unless_worth_it():
    table = pd.DataFrame({"3xc": [5.0, 6.0, 9.0, 5.0], "bboost": [12.0, 11.0, 16.0, 10.0],
                          "freehit": [3.0, 2.0, 1.0, 4.0]}, index=[12, 13, 14, 15])
    spread = chips.assign(table)
    assert len(set(spread.values())) == len(spread) == 3
    assert spread["bboost"] == 14 and spread["3xc"] == 13            # both best at 14: bench boost gains more
    # GW12 with expiry GW19: nothing is the best use of GW12, so nothing is proposed
    assert chips.propose(table, ["3xc", "bboost", "freehit"], 12, 19)[0] is None
    # GW14 is bench boost's best week and it clears 15: propose it
    assert chips.propose(table.loc[[14, 15]], ["3xc", "bboost", "freehit"], 14, 19)[0] == "bboost"
    # near expiry (2 weeks left, 3 chips): use it or lose it, even below the bar
    late = pd.DataFrame({"3xc": [5.0, 5.0], "bboost": [12.0, 8.0], "freehit": [1.0, 2.0]}, index=[18, 19])
    assert chips.propose(late, ["3xc", "bboost", "freehit"], 18, 19)[0] == "bboost"
    # a big Wildcard gain, or the last week, proposes the Wildcard
    wild = lambda value, gw: pd.DataFrame({"wildcard": [value]}, index=[gw])
    assert chips.propose(wild(14.0, 12), ["wildcard"], 12, 19)[0] == "wildcard"
    assert chips.propose(wild(1.0, 19), ["wildcard"], 19, 19)[0] == "wildcard"
    assert chips.propose(wild(5.0, 12), ["wildcard"], 12, 19)[0] is None


def test_wildcard_is_in_the_spread_and_pairs_with_bench_boost():
    """The Wildcard takes its own week like the other chips, so two can't land in one week, and a
    Wildcard the week before a Bench Boost is valued together (it builds the bench)."""
    table = pd.DataFrame({"wildcard": [9.0, 8.0, 7.0, float("nan")], "bboost": [13.0, 13.5, 14.0, 14.0],
                          "3xc": [6.0, 6.5, 6.0, 7.5]}, index=[20, 21, 22, 23])
    apart = chips.assign(table)
    assert apart == {"wildcard": 20, "bboost": 22, "3xc": 23}       # NaN (past the plan) is never picked
    pairs = {(20, 21): 9.0 + 19.0, (21, 22): 8.0 + 15.0}
    together = chips.assign(table, pairs)
    assert together["wildcard"] == 20 and together["bboost"] == 21 and together["3xc"] == 23
    assert chips.score(table, together, pairs) > chips.score(table, apart, pairs)
    # the Wildcard's worth this week counts the Bench Boost it sets up: 28 - 13.5 = 14.5, over the bar
    assert chips.value_now(table, together, "wildcard", 20, pairs) == 14.5
    assert chips.propose(table, ["wildcard", "bboost", "3xc"], 20, 38, pairs)[0] == "wildcard"
    assert chips.propose(table, ["wildcard", "bboost", "3xc"], 20, 38)[0] is None    # 9 alone isn't enough


def test_a_chip_week_you_agreed_to_is_kept_and_asked_about_when_it_comes(tmp_path, monkeypatch):
    import team
    monkeypatch.setattr(chips, "PLAN", tmp_path / "chip_plan.json")
    table = pd.DataFrame({"3xc": [5.0, 6.0, 9.0], "bboost": [12.0, 11.0, 16.0]}, index=[16, 17, 18])
    assert chips.assign(table)["bboost"] == 18
    assert chips.assign(table, fixed={"bboost": 17}) == {"bboost": 17, "3xc": 18}
    assert chips.propose(table.loc[[17, 18]], ["3xc", "bboost"], 17, 19, agreed={"bboost": 17})[0] == "bboost"
    # what's saved drops weeks that have passed and chips that are gone
    chips.save_plan({"bboost": 18, "3xc": 16}, {("bboost", 17), ("3xc", 12)})
    mine = team.Team(1, "x", [], 0, 1, {}, chips_used=[("wildcard", 3), ("3xc", 14)])
    assert chips.load_plan(mine, 15) == ({"bboost": 18}, {("bboost", 17)})
    assert chips.load_plan(mine, 19) == ({}, set())
    # the plan counts an agreed chip only in a later week: this week's is played only with its own yes
    import optimizer
    assert chips.with_plan(optimizer.Settings(), {"bboost": 18}, 15).bench_boost_gw == 18
    assert chips.with_plan(optimizer.Settings(), {"bboost": 15}, 15).bench_boost_gw is None
    assert chips.with_plan(optimizer.Settings(bench_boost_gw=15), {"bboost": 18}, 15).bench_boost_gw == 15


def test_news_is_used_only_when_fresh_and_for_this_gameweek(tmp_path, monkeypatch):
    monkeypatch.setattr(news, "NEWS", tmp_path / "news.json")
    monkeypatch.setattr(news, "HEARTBEAT", tmp_path / "heartbeat.json")
    now = DEADLINE - 2 * H
    overrides, lines = news.load(12, now)
    assert overrides == {} and "never reported" in "\n".join(lines)
    news.HEARTBEAT.write_text(json.dumps({"last_run": (now - 3 * H).isoformat()}))
    news.NEWS.write_text(json.dumps({"gw": 12, "checked_at": (now - 3 * H).isoformat(), "players": [
        {"id": 7, "name": "Rogers", "start_chance": 0.4, "reason": "Emery hinted at rotation", "source": "https://x"},
        {"id": 8, "name": "Junk", "start_chance": 3}]}))
    overrides, lines = news.load(12, now)
    assert overrides == {7: 0.4} and "Rogers: 40% to start" in "\n".join(lines) and "news check at" in lines[0]
    assert news.load(13, now)[0] == {}                                   # another gameweek's news
    assert news.load(12, now + 30 * H)[0] == {}                          # too old
    news.HEARTBEAT.write_text(json.dumps({"last_run": (now - 40 * H).isoformat()}))
    assert "may have stopped" in "\n".join(news.load(12, now)[1])


def test_chip_values_count_setting_the_team_up_for_them(monkeypatch):
    """Bench Boost re-planned with the chip in its week is worth at least the bench as it stands: with
    money and free transfers the plan can buy a bench that plays. Past the plan, the bench as it stands."""
    import team
    import numpy as np
    import test_optimizer
    from test_optimizer import GWS, cheapest_squad, make_pool
    monkeypatch.setattr(test_optimizer, "RNG", np.random.default_rng(7))   # the same pool whatever ran before
    players, xp = make_pool()
    squad = cheapest_squad(players)
    mine = team.Team(1, "x", squad, 20.0, 2, {})
    per_gw = xp.assign(**{"13": xp[12]}).rename(columns={"13": 13})       # one week past the 3-week plan
    found = chips.values(per_gw, players, mine, ["bboost", "3xc", "wildcard"], 19, GWS)
    assert list(found.table.index) == GWS + [13]
    for gw in GWS:
        assert found.table.loc[gw, "bboost"] >= found.unprepared.loc[gw, "bboost"] - 0.05   # weekly xp is rounded to 0.01
        assert found.table.loc[gw, "3xc"] >= found.unprepared.loc[gw, "3xc"] - 0.05
    assert found.table.loc[12, "bboost"] > found.unprepared.loc[12, "bboost"] + 0.3   # it buys a bench that plays
    assert found.table.loc[13, "bboost"] == found.unprepared.loc[13, "bboost"]       # past the plan: as it stands
    assert found.table.loc[13, "wildcard"] != found.table.loc[13, "wildcard"]        # NaN: not valued yet
    assert set(found.pairs) == {(10, 11), (10, 12), (11, 12)}


def test_a_chip_week_is_offered_only_when_setting_up_for_it_changes_this_week(monkeypatch):
    """The preview asks to plan around a Bench Boost week only when it would change this week's
    transfers, not for a week you said no to or already agreed to, and moves an agreed week only for
    chips.MOVE_BY more."""
    from types import SimpleNamespace
    import optimizer
    import recommend
    import team
    import numpy as np
    import test_optimizer
    from test_optimizer import GWS, cheapest_squad, make_pool
    monkeypatch.setattr(test_optimizer, "RNG", np.random.default_rng(7))   # the same pool whatever ran before
    players, xp = make_pool()
    mine = team.Team(1, "x", cheapest_squad(players), 20.0, 2, {})
    table = pd.DataFrame({"bboost": [10.0, 11.0, 14.0]}, index=GWS)
    analysis = SimpleNamespace(chip_plan={"bboost": 12}, chip_table=table, unprepared=table - 2)
    settings = optimizer.Settings(max_hits=0)
    prepared = recommend.plan_this_week(xp, players, mine, 10, chips.with_plan(settings, {"bboost": 12}, 10)).base.weeks[0]
    nothing = SimpleNamespace(transfers_out=[], transfers_in=[])
    offer = lambda week, agreed={}, declined=set(): recommend.chip_offers(
        analysis, xp, players, mine, 10, week, agreed, declined, settings)
    assert prepared.transfers_in                    # setting it up means buying this week, unlike `nothing`
    made = offer(nothing)
    assert len(made) == 1 and made[0]["kind"] == "commit" and made[0]["gw"] == 12
    assert made[0]["text"].startswith("Plan around Bench Boost in GW12?") and "instead of no transfers" in made[0]["text"]
    assert offer(prepared) == []                    # the same moves either way: ask later, if ever
    assert offer(nothing, declined={("bboost", 12)}) == []
    assert offer(nothing, agreed={"bboost": 12}) == []
    assert len(offer(nothing, agreed={"bboost": 11})) == 1           # 14 vs 11: worth moving
    table.loc[12, "bboost"] = 12.0
    assert offer(nothing, agreed={"bboost": 11}) == []                 # 12 vs 11: not worth asking
    analysis.chip_plan = {"bboost": 10}             # this week: that's the chip decision itself
    assert offer(nothing) == []
