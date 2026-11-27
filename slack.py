"""Posts the bot's messages to your Slack channel (ported from the ESPN bot's slack.py).

A bot token rather than an incoming webhook: a webhook can't post into a thread, and the reports
put their detail in replies under the message. From .env or the environment:
    SLACK_BOT_TOKEN   xoxb-..., the FPL Bot app's Bot User OAuth Token (OAuth & Permissions page)
    SLACK_CHANNEL     the channel's id, C followed by letters and digits (bottom of its About tab)
    SLACK_USER_ID     optional: your member ID, so a message that needs you @-mentions you
The app needs the chat:write scope and has to be invited to the channel (slack-app-manifest.yml).
"""
import json
import os
import re
import time

import requests

API = "https://slack.com/api/"
TRIES = 3
WAITS = (2, 10)        # seconds before the second and third try, unless Slack says how long
MAX_WAIT = 30
SECTION = 3000         # characters one section block may hold
HEADER = 150           # characters a header block may hold
MAX_BLOCKS = 50        # blocks one message may hold
MAX_ROWS = 100         # rows one table may hold (one table a message)
FENCE = "```"
LINK = re.compile(r"https?://[^\s<>|]+")
LAYOUT_ERRORS = {"invalid_blocks", "invalid_blocks_format", "msg_too_long"}   # worth a plainer try
CUT_SHORT = "Cut short: too long for one Slack message. The run's log has all of it."


class SlackError(Exception):
    """refused: Slack answered and said no (a bad token, a layout it won't take), rather than never
    answering. Only a refused layout is worth posting again in a plainer one."""

    def __init__(self, message, refused=False):
        super().__init__(message)
        self.refused = refused


def configured():
    return bool(os.getenv("SLACK_BOT_TOKEN") and os.getenv("SLACK_CHANNEL"))


def call(method, fields, sleep=None):
    """One Slack Web API call. Returns Slack's answer, or raises SlackError.

    Tried three times when the failure may pass: no connection, a timeout, a 5xx, or a 429 (after
    the wait Slack asks for). Slack's own refusals, such as a bad token or a channel the bot isn't
    in, are raised at once. A timeout can hide a post that landed, so very rarely a message may
    arrive twice; better than not at all.
    """
    sleep = sleep or time.sleep
    headers = {"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}",
               "Content-Type": "application/json; charset=utf-8"}
    payload = json.dumps(fields).encode()
    problem = None
    for attempt in range(TRIES):
        wait = WAITS[min(attempt, len(WAITS) - 1)]
        try:
            resp = requests.post(API + method, data=payload, headers=headers, timeout=20)
        except requests.exceptions.RequestException as error:
            problem = f"{type(error).__name__}: {error}"
        else:
            if resp.status_code == 429 or resp.status_code >= 500:
                problem = f"HTTP {resp.status_code}"
                if resp.status_code == 429:
                    wait = min(_seconds(resp.headers.get("Retry-After"), wait), MAX_WAIT)
            else:
                try:
                    answer = resp.json()
                except ValueError:
                    raise SlackError(f"{method}: HTTP {resp.status_code}, not JSON: {resp.text[:100]}")
                if not answer.get("ok"):
                    raise SlackError(f"{method}: {answer.get('error', 'refused')}", refused=True)
                return answer
        if attempt + 1 < TRIES:
            sleep(wait)
    raise SlackError(f"{method}: {problem} ({TRIES} tries)")


def _seconds(value, fallback):
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return fallback


def post(text, blocks=None, thread_ts=None):
    """Post one message to SLACK_CHANNEL and return its ts (Slack's id for it). text is what a
    phone's notification shows; blocks, when given, are what the channel shows; thread_ts puts it
    in the thread under that message."""
    message = {"channel": os.environ["SLACK_CHANNEL"], "text": escape(text),
               "unfurl_links": False, "unfurl_media": False}
    if blocks:
        message["blocks"] = blocks
    if thread_ts:
        message["thread_ts"] = thread_ts
    return call("chat.postMessage", message)["ts"]


def post_first(text, layouts, thread_ts=None):
    """Post the first of these layouts that Slack takes, and return its ts.

    A layout Slack refuses (invalid_blocks: a section over its length limit, say) falls back to the
    next one, so a mistake in the formatting costs the formatting, never the message. Anything else
    (no answer, a bad token, not_in_channel) is raised at once: a plainer layout wouldn't get through either.
    """
    for number, blocks in enumerate(layouts, 1):
        try:
            return post(text, blocks, thread_ts)
        except SlackError as error:
            if not (error.refused and str(error).split(": ", 1)[-1] in LAYOUT_ERRORS) or number == len(layouts):
                raise
            print(f"(Slack refused the layout ({error}); posting it plainer)")


def escape(text):
    """Slack reads &, < and > as markup, in code blocks too."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def mention():
    """<@you> when SLACK_USER_ID is set, so the message notifies you whatever the channel's setting."""
    user = os.getenv("SLACK_USER_ID", "").strip()
    return f"<@{user}>" if user else ""


def plain(subject, body):
    """The subject as a header over the body in monospace, so tables line up as in the email's text
    version: for messages without a layout of their own, and when Slack refuses a layout."""
    return [header(subject)] + monospace(body, MAX_BLOCKS - 1)


def monospace(body, room=MAX_BLOCKS):
    """The body in monospace sections, then its links, which can't be clicked inside monospace,
    in at most room blocks."""
    chunks = _chunks(body.strip("\n").replace(FENCE, "'''"), SECTION - len(f"{FENCE}\n\n{FENCE}")) \
        if body.strip() else []
    links = _links(body)
    fits = room - bool(links) - 1      # leaves room for the cut-short note
    blocks = [section(f"{FENCE}\n{chunk}\n{FENCE}") for chunk in chunks[:fits]]
    if links:
        blocks.append(section(links))
    if len(chunks) > fits:
        blocks.append(context(CUT_SHORT))
    return blocks


def header(text):
    return {"type": "header", "text": {"type": "plain_text", "text": text[:HEADER]}}


def section(text):
    """Text in Slack's markup (mrkdwn), escaped already: &, < and > that aren't markup as &amp; etc."""
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def sections(text):
    """Text in Slack's markup, escaped already, in as many sections as it needs, cut at line ends."""
    return [section(chunk) for chunk in _chunks(text, SECTION, escaped=True)]


def context(text):
    """Small grey text, escaped already."""
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def table(rows, right=()):
    """A table, its first row the header. Slack takes one a message, of up to 20 columns and 100 rows,
    and shows it at the bottom of the message; right lists the columns (from 0) to align right, as
    numbers read best, and the others wrap rather than cut long text. An empty cell shows a dash."""
    return {"type": "table",
            "column_settings": [{"align": "right"} if number in right else {"align": "left", "is_wrapped": True}
                                for number in range(len(rows[0]))],
            "rows": [[{"type": "raw_text", "text": str(cell) or "-"} for cell in row] for row in rows[:MAX_ROWS]]}


def link(url, title):
    return f"<{escape(url)}|{escape(title).replace('|', '-')}>"


def _chunks(text, room, escaped=False):
    """The text, escaped, in pieces that each fit a section, cut at line ends where it can be."""
    lines = []
    for line in text.split("\n"):
        if escaped:
            while len(line) > room:
                lines.append(line[:room])
                line = line[room:]
            lines.append(line)
            continue
        while len(escape(line)) > room:
            piece = room // len("&amp;")               # escaping makes a piece at most this much longer
            lines.append(escape(line[:piece]))
            line = line[piece:]
        lines.append(escape(line))
    chunks, current = [], []
    for line in lines:
        if current and len("\n".join(current + [line])) > room:
            chunks.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        chunks.append("\n".join(current))
    return chunks


def _links(body):
    """Each link in the body once, in order, as clickable lines that fit one section."""
    found = []
    for url in LINK.findall(body):
        url = url.rstrip(".,;:)]'\"")
        if url not in found:
            found.append(url)
    lines = []
    for url in found:
        line = f"• <{escape(url)}>"
        if len("\n".join(lines + [line])) > SECTION:
            break
        lines.append(line)
    return "\n".join(lines)
