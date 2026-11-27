"""Your overrides (overrides.py): reading them from Slack, applying them to the plan, and the /fpl
commands. python3 -m pytest test_overrides.py. Nothing here reaches Slack, FPL or GitHub."""
import pandas as pd
import pytest

import doc
import optimizer
import overrides
import slack

NOW = 1764356400                                         # Sat 28 Nov 2026 14:00 ET
BOOT = {
    "events": [{"id": 12, "is_next": False, "deadline_time": "2026-11-27T18:30:00Z"},
               {"id": 13, "is_next": True, "deadline_time": "2026-12-01T18:30:00Z"}],
    "teams": [{"id": 1, "short_name": "ARS"}, {"id": 2, "short_name": "MCI"}, {"id": 3, "short_name": "NFO"}],
    "elements": [
        {"id": 17, "web_name": "Saka", "first_name": "Bukayo", "second_name": "Saka", "team": 1, "element_type": 3,
         "total_points": 80},
        {"id": 9, "web_name": "Haaland", "first_name": "Erling", "second_name": "Haaland", "team": 2, "element_type": 4,
         "total_points": 90},
        {"id": 4, "web_name": "Gabriel", "first_name": "Gabriel", "second_name": "dos Santos Magalhães", "team": 1,
         "element_type": 2, "total_points": 60},
        {"id": 5, "web_name": "Murillo", "first_name": "Murillo", "second_name": "Santiago Costa dos Santos",
         "team": 3, "element_type": 2, "total_points": 40},
        {"id": 6, "web_name": "Ødegaard", "first_name": "Martin", "second_name": "Ødegaard", "team": 1,
         "element_type": 3, "total_points": 50},
    ]}


def record(key, choice, at):
    return {"type": "context", "block_id": f"answer-{key}-{choice}-{at}", "elements": []}


# ---------------------------------------------------------------- reading and applying

def test_the_latest_record_for_each_key_counts_and_only_this_gameweeks():
    messages = [{"blocks": [record("keep.17.gw13", "on", 100), record("captain.gw13", "9", 100)]},
                {"blocks": [record("keep.17.gw13", "off", 200), record("ban.9.gw13", "on", 150),
                            record("skip.1.2.gw13", "on", 120), record("keep.5.gw12", "on", 300)]},
                {"blocks": [record("captain.gw13", "0", 90), record("1", "yes", 999)]}]
    found = overrides.parse(overrides.latest(messages), 13)
    assert found == overrides.Overrides(keep=set(), ban={9}, skip={(1, 2)}, captain=9)


def test_overrides_become_locked_banned_and_a_fixed_captain():
    base = optimizer.Settings(max_hits=0, locked={3})
    mine = overrides.Overrides(keep={17}, ban={9}, skip={(1, 2)}, captain=6)
    settings = overrides.settings(base, mine, 13)
    assert settings.locked == {3, 17, 1} and settings.banned == {9, 2} and settings.captain == {13: 6}
    assert settings.max_hits == 0 and overrides.settings(base, overrides.Overrides(), 13) == base


def test_a_captain_pick_stands_only_if_he_is_in_the_squad_and_fit():
    players = pd.DataFrame({"id": [6, 9, 17], "web_name": ["Ødegaard", "Haaland", "Saka"],
                            "status": ["a", "a", "d"], "chance": [100.0, 100.0, 50.0]})
    notes = []
    assert overrides.checked(overrides.Overrides(captain=6), players, [6, 17], notes).captain == 6
    assert overrides.checked(overrides.Overrides(captain=9), players, [6, 17], notes).captain is None
    assert overrides.checked(overrides.Overrides(captain=17), players, [6, 17], notes).captain is None
    assert notes == ["Your captain pick, Haaland, isn't in your squad, so the bot's pick stands.",
                     "Your captain pick, Saka, is only 50% to play, so the bot's pick stands."]


def test_describe_says_what_you_set():
    players = pd.DataFrame({"id": [1, 2, 9, 17], "web_name": ["Raya", "Trafford", "Haaland", "Saka"]})
    mine = overrides.Overrides(keep={17}, ban={9}, skip={(1, 2)}, captain=17)
    assert overrides.describe(mine, players) == "keep Saka; don't buy Haaland; skip Raya → Trafford; captain Saka"
    assert overrides.describe(overrides.Overrides(), players) == ""


def test_the_preview_offers_skip_by_each_transfer_and_a_captain_menu():
    name = {1: "Raya", 2: "Trafford", 5: "N.Jackson", 9: "Haaland"}
    message = doc.Message("GW13 preview", blocks=[doc.Callout("Change the bot's moves (optional)", overrides.preview_lines(
        "fpl-abc123", 13, [(1, 2)], [5, 9], None, name), "info", slack_only=True)])
    blocks = doc.to_slack(message)[0]["layouts"][0]
    actions = [b for b in blocks if b["type"] == "actions"]
    assert [e["value"] for e in actions[0]["elements"]] == ["fpl-abc123|skip.1.2.gw13|on", "fpl-abc123|skip.1.2.gw13|off"]
    menu = actions[1]["elements"][0]
    assert menu["type"] == "static_select" and menu["initial_option"]["value"] == "fpl-abc123|captain.gw13|5"
    assert [o["text"]["text"] for o in menu["options"]] == ["N.Jackson (the bot’s pick)", "Haaland"]
    assert "Change the bot" not in doc.to_text(message) and "Change the bot" not in doc.to_html(message)


# ---------------------------------------------------------------- /fpl commands

@pytest.fixture
def channel(monkeypatch):
    """Records what the bot posts; the channel holds messages."""
    posts, held = [], []
    monkeypatch.setattr(slack, "post", lambda text, blocks=None, thread_ts=None: posts.append((text, blocks)) or "5.5")
    monkeypatch.setattr(slack, "post_first", lambda text, layouts, thread_ts=None: posts.append((text, layouts[0])) or "6.6")
    monkeypatch.setattr(overrides, "channel_messages", lambda now=None: held)
    return posts, held


def test_names_are_found_without_accents_and_narrowed_by_club():
    assert [p["id"] for p in overrides.find("odegaard", BOOT)] == [6]
    assert [p["id"] for p in overrides.find("haaland", BOOT)] == [9]
    assert [p["id"] for p in overrides.find("santos", BOOT)] == [4, 5]           # both, best first
    assert [p["id"] for p in overrides.find("santos NFO", BOOT)] == [5]
    assert overrides.find("Pelé", BOOT) == []


def test_keep_posts_a_confirmation_that_is_also_the_record(channel):
    posts, _ = channel
    assert overrides.command("keep saka", BOOT, NOW)
    text, blocks = posts[0]
    assert text == "🔒 Keep Saka (ARS MID) for GW13: the bot won't sell him."
    assert blocks[1]["block_id"].startswith("answer-keep.17.gw13-on-")
    assert blocks[2]["elements"][0]["value"] == "fpl-gw13|keep.17.gw13|off"
    assert overrides.parse(overrides.latest([{"blocks": blocks}]), 13).keep == {17}


def test_ban_and_captain(channel):
    posts, _ = channel
    assert overrides.command("ban Haaland", BOOT, NOW) and overrides.command("captain Saka", BOOT, NOW)
    found = overrides.parse(overrides.latest([{"blocks": b} for _, b in posts]), 13)
    assert found.ban == {9} and found.captain == 17
    assert posts[1][1][2]["elements"][0]["value"] == "fpl-gw13|captain.gw13|0"     # Undo: the bot's pick


def test_an_ambiguous_or_unknown_name_asks_and_records_nothing(channel):
    posts, _ = channel
    assert not overrides.command("keep santos", BOOT, NOW)
    assert posts[0][0].startswith("/fpl keep santos: which one? Gabriel (ARS DEF), Murillo (NFO DEF)")
    assert not overrides.command("ban Pelé", BOOT, NOW)
    assert "no player called" in posts[1][0]
    assert all(blocks is None for _, blocks in posts)


def test_too_close_to_the_deadline_says_to_use_the_fpl_site(channel):
    posts, _ = channel
    deadline = pd.Timestamp("2026-12-01T18:30:00Z").timestamp()
    assert not overrides.command("keep Saka", BOOT, deadline - 2 * 3600)
    assert posts[0][0].startswith("Too late for GW13")


def test_list_says_what_you_have_set(channel):
    posts, held = channel
    held += [{"blocks": [record("keep.17.gw13", "on", 100), record("ban.9.gw13", "on", 100)]}]
    assert overrides.command("list", BOOT, NOW)
    assert posts[0][0] == "Your overrides for GW13: keep Saka; don't buy Haaland."
    held.clear()
    overrides.command("list", BOOT, NOW)
    assert posts[1][0] == "Your overrides for GW13: none yet: the bot goes ahead with its own plan."


def test_explain_posts_the_breakdown_in_a_thread(channel, monkeypatch):
    posts, _ = channel
    ran = []

    class Run:
        returncode, stdout, stderr = 0, "Haaland: 7.1 xP a match", ""
    monkeypatch.setattr(overrides.subprocess, "run", lambda args, **kw: ran.append(args) or Run())
    assert overrides.command("explain haaland", BOOT, NOW)
    assert ran[0][-1] == "Haaland" or "Haaland" in ran[0]
    assert posts[0][0] == "Explain: Haaland (MCI FWD)"
    assert "Haaland: 7.1 xP a match" in posts[1][1][0]["text"]["text"]


def test_an_unknown_command_says_what_works(channel):
    posts, _ = channel
    assert not overrides.command("sell Saka", BOOT, NOW)
    assert posts[0][0].startswith('I don\'t know "/fpl sell Saka"')
