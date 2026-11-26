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


def test_each_post_has_at_most_one_table_and_the_detail_goes_in_the_thread():
    message = doc.Message("GW12 preview", "Deadline Fri 27 Nov 13:30 ET", [("Live: changes are sent", "good")], [
        doc.Callout("Team news", ["Raya: shoulder"], "warn"),
        doc.Heading("At the main run"), doc.Para("Transfers"), doc.Table(["Out", "In"], [["Raya", "Trafford"]]),
        doc.Heading("Starting XI"), doc.Table(["Player"], [["Saka"]]), doc.Para("Bench: x"),
        doc.Heading("Plan"), doc.Table(["GW"], [["13"]]), doc.Table(["Chip"], [["BB"]])],
        footer="Run: https://github.com/x/y/actions/runs/1")
    posts = doc.to_slack(message)
    assert [p["text"] for p in posts] == ["GW12 preview", "Starting XI", "Plan", "GW12 preview"]
    for post in posts:
        assert sum(b["type"] == "table" for b in blocks_of(post)) <= 1
    main = blocks_of(posts[0])
    assert main[0] == slack.header("GW12 preview")
    assert "🟢 Live: changes are sent" in main[1]["elements"][0]["text"]
    assert main[2] == slack.section(":warning: *Team news*\nRaya: shoulder")
    assert "<https://github.com/x/y/actions/runs/1>" in main[-1]["elements"][0]["text"]


def test_an_alert_is_flagged_and_without_headings_is_one_post():
    posts = doc.to_slack(doc.Message("GW12 main run: ACTION NEEDED", blocks=[
        doc.Callout("Action needed", ["FAILED: HTTP 400", doc.Tone("Make the changes yourself.", bold=True)], "bad")],
        alert=True))
    assert len(posts) == 1
    assert blocks_of(posts[0])[0] == slack.header("🚨 GW12 main run: ACTION NEEDED")
    assert blocks_of(posts[0])[1] == slack.section(":rotating_light: *Action needed*\nFAILED: HTTP 400\n"
                                                   "*Make the changes yourself.*")


def test_badges_buttons_and_muted_text():
    posts = doc.to_slack(doc.Message("GW12 preview", blocks=[
        doc.Para(["Saka", doc.Badge("C")]),
        doc.Para([doc.Button("Yes", "mailto:x@gmail.com?subject=FPL%20decision&body=1%20yes", "good", plain="reply")]),
        doc.Para("None this week.", tone="muted")]))
    main = blocks_of(posts[0])
    assert main[1] == slack.section("Saka `C`")
    assert main[2]["text"]["text"].startswith("<mailto:x@gmail.com?subject=FPL%20decision&amp;body=1%20yes|Yes>")
    assert main[3] == slack.context("None this week.")


def test_bold_keeps_its_spaces_outside_so_slack_shows_it_and_a_leading_badge_has_no_space():
    posts = doc.to_slack(doc.Message("t", blocks=[
        doc.Para([doc.Tone("Bench: ", bold=True), "Trafford 3.3"]),
        doc.Para([doc.Badge("DO", "good"), "  ", doc.Tone("Transfers", bold=True), ": free"])]))
    assert blocks_of(posts[0])[1] == slack.section("*Bench:* Trafford 3.3")
    assert blocks_of(posts[0])[2] == slack.section("`DO`  *Transfers*: free")


def test_table_cells_mark_colour_with_a_dot_and_badges_in_brackets():
    posts = doc.to_slack(doc.Message("t", blocks=[doc.Heading("Fixtures"), doc.Table(
        ["Player", "GW12"], [[["Tarkowski", doc.Badge("OUT", "bad")],
                              [doc.Tone("ARS (H)", "good"), " ", doc.Tone("out", "bad", bold=True)]]], right={1},
        note="Green: easier.")]))
    table = blocks_of(posts[0])[-1]
    assert table["rows"][1] == [{"type": "raw_text", "text": "Tarkowski (OUT)"},
                                {"type": "raw_text", "text": "🟢 ARS (H) 🔴 out"}]
    assert table["column_settings"] == [{"align": "left", "is_wrapped": True}, {"align": "right"}]
    assert blocks_of(posts[0])[-2] == slack.context("Green: easier.")


def test_a_long_callout_is_split_into_sections_that_fit():
    lines = [f"line {n} " + "x" * 90 for n in range(100)]
    blocks = blocks_of(doc.to_slack(doc.Message("t", blocks=[doc.Callout("Where it failed", lines)]))[0])
    texts = [b["text"]["text"] for b in blocks if b["type"] == "section"]
    assert len(texts) > 1 and all(len(t) <= slack.SECTION for t in texts)
    assert "line 99" in texts[-1]
