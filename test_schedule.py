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


def test_reading_your_reply():
    owners = {"me@gmail.com"}
    code = "fpl-abc123"
    quoted = "\n\nOn Thu, 26 Nov 2026 at 13:43, me@gmail.com wrote:\n> NEEDS YOUR OK ... reply YES"
    assert approvals.verdict([reply(f"Re: FPL: GW12 preview [{code}]", "Yes please" + quoted)], code, owners) == "yes"
    assert approvals.verdict([reply(f"RE: FPL: GW12 preview [{code}]", "\n\nok" + quoted)], code, owners) == "yes"
    assert approvals.verdict([reply(f"Re: FPL: GW12 preview [{code}]", "no thanks" + quoted)], code, owners) == "no"
    assert approvals.verdict([reply(f"Re: FPL: GW12 preview [{code}]", "maybe?" + quoted)], code, owners) is None
    # the preview itself (not a reply), someone else, another code, and only-quoted text don't count
    assert approvals.verdict([reply(f"FPL: GW12 preview [{code}]", "YES")], code, owners) is None
    assert approvals.verdict([reply(f"Re: FPL: GW12 preview [{code}]", "yes", "x@evil.com")], code, owners) is None
    assert approvals.verdict([reply("Re: FPL: GW12 preview [fpl-zzz999]", "yes")], code, owners) is None
    assert approvals.verdict([reply(f"Re: [{code}]", "> yes\n> quoted only")], code, owners) is None
    # the latest answer wins
    both = [reply(f"Re: [{code}]", "yes"), reply(f"Re: [{code}]", "no, changed my mind")]
    assert approvals.verdict(both, code, owners) == "no"


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
    assert chips.propose(pd.DataFrame(), ["wildcard"], 12, 19, wild=14.0)[0] == "wildcard"
    assert chips.propose(pd.DataFrame(), ["wildcard"], 19, 19, wild=1.0)[0] == "wildcard"
    assert chips.propose(pd.DataFrame(), ["wildcard"], 12, 19, wild=5.0)[0] is None


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
