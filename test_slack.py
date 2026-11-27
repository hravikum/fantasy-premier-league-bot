"""Posting to Slack, mailer.send choosing between Slack and email, and doc.to_slack's layout.
python3 -m pytest test_slack.py. Nothing here reaches Slack or Gmail."""
import json

import pytest
import requests

import doc
import mailer
import slack


class Reply:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self.headers = status, headers or {}
        self._body = {"ok": True, "ts": "1.2"} if body is None else body
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


@pytest.fixture
def env(monkeypatch):
    """Slack and Gmail set, though fake, so values in .env can't reach a test (load_dotenv never overrides)."""
    for key, value in dict(SLACK_BOT_TOKEN="xoxb-test", SLACK_CHANNEL="C0TEST", SLACK_USER_ID="", EMAIL_TOO="",
                           GMAIL_ADDRESS="x@gmail.com", GMAIL_APP_PASSWORD="abcd efgh ijkl mnop").items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(slack.time, "sleep", lambda s: None)
    return monkeypatch


@pytest.fixture
def fake_slack(env):
    """Slack gives each reply in turn, the last one again after that (an exception is raised rather
    than returned). Returns the requests it was sent and a way to set the replies."""
    sent, replies = [], [Reply()]

    def post(url, data=None, headers=None, timeout=None):
        sent.append({"url": url, "body": json.loads(data), "headers": headers})
        reply = replies[min(len(sent), len(replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply

    env.setattr(requests, "post", post)

    def answer(*new):
        replies[:] = new
    return sent, answer


@pytest.fixture
def inbox(env):
    emails = []
    env.setattr(mailer, "_email", lambda subject, body, html: emails.append(subject) or True)
    return emails


# ---------------------------------------------------------------- slack.py

def test_a_post_goes_to_the_channel_with_the_token_and_without_previews(fake_slack):
    sent, _ = fake_slack
    assert slack.post("GW12 preview", [slack.section("hi")]) == "1.2"
    request = sent[0]
    assert request["url"].endswith("/chat.postMessage")
    assert request["headers"]["Authorization"] == "Bearer xoxb-test"
    assert request["body"]["channel"] == "C0TEST" and request["body"]["unfurl_links"] is False


def test_a_dropped_connection_and_a_server_error_are_tried_again(fake_slack):
    sent, answer = fake_slack
    answer(requests.exceptions.ConnectionError("down"), Reply(503), Reply())
    assert slack.post("x") == "1.2" and len(sent) == 3


def test_three_failures_give_up_with_the_last_reason(fake_slack):
    sent, answer = fake_slack
    answer(Reply(500))
    with pytest.raises(slack.SlackError, match="HTTP 500"):
        slack.post("x")
    assert len(sent) == 3


def test_slacks_own_refusal_is_not_tried_again(fake_slack):
    sent, answer = fake_slack
    answer(Reply(body={"ok": False, "error": "not_in_channel"}))
    with pytest.raises(slack.SlackError, match="not_in_channel") as error:
        slack.post("x")
    assert error.value.refused and len(sent) == 1


def test_a_body_too_long_for_one_message_says_it_was_cut():
    blocks = slack.plain("subject", "line\n" * 40000)
    assert len(blocks) == slack.MAX_BLOCKS and blocks[-1]["elements"][0]["text"] == slack.CUT_SHORT
    assert all(len(b["text"]["text"]) <= slack.SECTION for b in blocks if b["type"] == "section")


# ---------------------------------------------------------------- mailer.send

def test_with_slack_set_up_a_message_goes_to_slack_alone(fake_slack, inbox):
    sent, _ = fake_slack
    assert mailer.send("GW12 preview", "body")
    assert len(sent) == 1 and inbox == []


def test_while_email_too_is_true_it_goes_both_ways(fake_slack, inbox, env):
    env.setenv("EMAIL_TOO", "true")
    assert mailer.send("GW12 preview", "body")
    assert len(fake_slack[0]) == 1 and inbox == ["GW12 preview"]


def test_without_slack_it_emails_as_before(fake_slack, inbox, env):
    env.delenv("SLACK_BOT_TOKEN")
    assert mailer.send("GW12 preview", "body")
    assert fake_slack[0] == [] and inbox == ["GW12 preview"]


def test_when_slack_is_down_it_emails_instead(fake_slack, inbox):
    _, answer = fake_slack
    answer(Reply(body={"ok": False, "error": "invalid_auth"}))
    assert mailer.send("GW12 preview", "body")
    assert inbox == ["GW12 preview"]


def test_an_alert_goes_both_ways_and_mentions_you(fake_slack, inbox, env):
    env.setenv("SLACK_USER_ID", "U123")
    sent, _ = fake_slack
    assert mailer.send("ACTION NEEDED: GW12 main run couldn't make its changes", "HTTP 400", urgent=True)
    assert inbox == ["ACTION NEEDED: GW12 main run couldn't make its changes"]
    body = sent[0]["body"]
    assert body["text"].startswith("&lt;@U123&gt;") or body["text"].startswith("<@U123>")
    assert body["blocks"][1] == slack.section("<@U123>")


def test_a_layout_slack_refuses_is_posted_plain_instead(fake_slack, inbox):
    sent, answer = fake_slack
    answer(Reply(body={"ok": False, "error": "invalid_blocks"}), Reply())
    posts = doc.to_slack(doc.Message("GW12 preview", blocks=[doc.Para("hello")]))
    assert mailer.send("GW12 preview", "hello", slack_posts=posts)
    assert sent[1]["body"]["blocks"] == posts[0]["layouts"][1] and inbox == []


def test_a_bot_not_in_the_channel_is_not_tried_plainer_and_the_message_is_emailed(fake_slack, inbox):
    sent, answer = fake_slack
    answer(Reply(body={"ok": False, "error": "not_in_channel"}))
    posts = doc.to_slack(doc.Message("GW12 preview", blocks=[doc.Para("hello")]))
    assert mailer.send("GW12 preview", "hello", slack_posts=posts)
    assert len(sent) == 1 and inbox == ["GW12 preview"]


def test_the_report_goes_in_the_thread_and_a_lost_reply_still_counts(fake_slack, inbox):
    sent, answer = fake_slack
    message = doc.Message("GW12 preview", blocks=[doc.Heading("Changes"), doc.Para("x"), doc.Heading("Fixtures"),
                                                   doc.Table(["Player"], [["Saka"]])])
    answer(Reply(), Reply(500))
    assert mailer.send("GW12 preview", "x", slack_posts=doc.to_slack(message))
    assert sent[1]["body"]["thread_ts"] == "1.2" and inbox == []


# ---------------------------------------------------------------- doc.to_slack

def blocks_of(post):
    return post["layouts"][0]


def texts(blocks):
    """Each block's text, for comparing: sections and context as they read, a divider as ---."""
    return [b["text"]["text"] if "text" in b else b["elements"][0]["text"] if b["type"] == "context" else "---"
            for b in blocks]


def test_the_summary_is_in_the_channel_and_the_report_in_one_reply_between_dividers():
    message = doc.Message("GW12 preview", "Deadline Fri 27 Nov 13:30 ET", [("Live: changes are sent", "good")], [
        doc.Callout("Team news", ["Raya: shoulder"], "warn"),
        doc.Heading("At the main run"), doc.Para("Transfers"), doc.Table(["Out", "In"], [["Raya", "Trafford"]]),
        doc.Heading("Starting XI"), doc.Table(["Player"], [["Saka"]]), doc.Para("Bench: x"),
        doc.Heading("Plan"), doc.Table(["GW"], [["13"]]), doc.Table(["Chip"], [["BB"]])],
        footer="Run: https://github.com/x/y/actions/runs/1")
    posts = doc.to_slack(message)
    assert [p["text"] for p in posts] == ["GW12 preview", "GW12 preview: the full report"]
    assert all(b["type"] != "table" for p in posts for b in blocks_of(p))
    assert texts(blocks_of(posts[0])) == [
        "GW12 preview", "Deadline Fri 27 Nov 13:30 ET\n🟢 Live: changes are sent", "---",
        ":warning: *Team news*\nRaya: shoulder", "---", "*At the main run*", "Transfers", "*Raya*  Trafford", "---",
        "🧵 The full report is in the thread · <https://github.com/x/y/actions/runs/1|Run log>"]
    assert texts(blocks_of(posts[1])) == ["👕  *Starting XI*", "*Saka*", "Bench: x", "---", "🗓️  *Plan*", "*13*", "*BB*"]


def test_a_report_too_long_for_one_reply_is_split_between_sections():
    blocks = [doc.Heading("Changes")]
    for n in range(30):
        blocks += [doc.Heading(f"Section {n}"), doc.Para("a"), doc.Para("b")]
    posts = doc.to_slack(doc.Message("GW12 preview", blocks=blocks))
    assert len(posts) == 4 and all(len(blocks_of(p)) <= slack.MAX_BLOCKS for p in posts)
    assert blocks_of(posts[2])[0] == slack.section("*Section 12*")      # 12 sections of 4 blocks a reply


def test_an_alert_is_flagged_and_without_headings_is_one_post():
    posts = doc.to_slack(doc.Message("GW12 main run: ACTION NEEDED", blocks=[
        doc.Callout("Action needed", ["FAILED: HTTP 400", doc.Tone("Make the changes yourself.", bold=True)], "bad")],
        alert=True))
    assert len(posts) == 1
    assert texts(blocks_of(posts[0])) == ["🚨 GW12 main run: ACTION NEEDED", "---",
                                          ":rotating_light: *Action needed*\nFAILED: HTTP 400\n"
                                          "*Make the changes yourself.*"]


def test_badges_buttons_muted_text_and_bold_spaces():
    posts = doc.to_slack(doc.Message("GW12 preview", blocks=[
        doc.Para(["Saka", doc.Badge("C")]),
        doc.Para([doc.Button("Yes", "mailto:x@gmail.com?subject=FPL%20decision&body=1%20yes", "good", plain="reply")]),
        doc.Para("None this week.", tone="muted"),
        doc.Para([doc.Tone("Bench: ", bold=True), "Trafford 3.3"]),
        doc.Para([doc.Badge("DO", "good"), "  ", doc.Tone("Transfers", bold=True), ": free"])]))
    shown = texts(blocks_of(posts[0]))[2:]
    assert shown[0] == "Saka `C`"
    assert shown[1].startswith("<mailto:x@gmail.com?subject=FPL%20decision&amp;body=1%20yes|Yes>")
    assert blocks_of(posts[0])[4] == slack.context("None this week.")
    assert shown[3:] == ["*Bench:* Trafford 3.3", "`DO`  *Transfers*: free"]


def test_slack_lines_replace_a_table_or_a_paragraph():
    posts = doc.to_slack(doc.Message("t", blocks=[doc.Heading("Fixtures"), doc.Table(
        ["Player", "GW12"], [["Tarkowski", "ARS (H) out"]], note="Green: easier.",
        slack=[[doc.Tone("Tarkowski", bold=True), doc.Badge("OUT", "bad"), f"  {doc.dot('good')} ARS (H) out"]]),
        doc.Para("starts: A; captain: B -> C", slack=[["• starts: A"], ["• captain: B → C"]])]))
    # The lines say what the note would, so the note isn't shown.
    assert texts(blocks_of(posts[0]))[2:5] == ["📅  *Fixtures*", "*Tarkowski* `OUT`  🟢 ARS (H) out",
                                               "• starts: A\n• captain: B → C"]


def test_a_table_without_lines_of_its_own_is_a_line_a_row():
    posts = doc.to_slack(doc.Message("t", blocks=[doc.Heading("Top projections, GW12-17"), doc.Table(
        ["Out", "", "In"], [["Rogers £8.2", "→", "B.Fernandes £11.6"], ["GK", "", "Trafford £5.1 (23.3)"]])]))
    assert texts(blocks_of(posts[0]))[3] == "*Rogers £8.2*  →  B.Fernandes £11.6\n*GK*  Trafford £5.1 (23.3)"


def test_a_real_table_ends_its_reply_and_the_next_section_starts_a_new_one():
    fixtures = doc.Table(["Player", "GW12", "xP"], [["Raya", "NFO (A) 3.6", "10.9"]], note="long note",
                         slack=[["_🟢 easier_"]], slack_rows=[["Player", "GW12", "xP"], ["Raya", "🟢 NFO (A) 3.6", "10.9"]])
    top = doc.Table(["", "Expected points"], [["GK", "Raya £6.2 (22.0)"]],
                    slack_rows=[["Pos", "Player", "Price", "xP"], ["GK", "Raya", "£6.2", "22.0"]])
    posts = doc.to_slack(doc.Message("GW12 preview", blocks=[
        doc.Heading("Changes"), doc.Para("x"),
        doc.Heading("Transfers"), doc.Para("t"), doc.Heading("Fixtures"), fixtures,
        doc.Heading("Starting XI"), doc.Para("xi"), doc.Heading("Top projections"), top]))
    assert len(posts) == 3
    first, second = blocks_of(posts[1]), blocks_of(posts[2])
    assert texts(first[:-1]) == ["🔁  *Transfers*", "t", "---", "📅  *Fixtures*", "_🟢 easier_"]
    assert first[-1]["type"] == "table" and first[-1]["rows"][1][1]["text"] == "🟢 NFO (A) 3.6"
    assert [c["align"] for c in first[-1]["column_settings"]] == ["left", "left", "right"]
    assert texts(second[:-1]) == ["👕  *Starting XI*", "xi", "---", "📈  *Top projections*"]
    assert [c["align"] for c in second[-1]["column_settings"]] == ["left", "left", "right", "right"]


def test_a_long_callout_is_split_into_sections_that_fit():
    lines = [f"line {n} " + "x" * 90 for n in range(100)]
    blocks = blocks_of(doc.to_slack(doc.Message("t", blocks=[doc.Callout("Where it failed", lines)]))[0])
    texts = [b["text"]["text"] for b in blocks if b["type"] == "section"]
    assert len(texts) > 1 and all(len(t) <= slack.SECTION for t in texts)
    assert "line 99" in texts[-1]
