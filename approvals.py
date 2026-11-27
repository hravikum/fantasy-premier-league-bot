"""Asking you first: each hit or chip is its own numbered decision in the preview.

Answer each one, any of these ways:
  - in Slack, tap its Yes or No: slack-worker/worker.js writes your answer into the message
  - in the email, tap its Yes or No: it opens a ready-made email to the bot, you press Send
  - or reply to the preview email with one answer per decision: "1 yes" / "2 no" (any order, any
    lines), or just the answers in the order listed ("yes, no"). With one decision, "yes" is enough.
A decision you don't answer is skipped. A later answer replaces an earlier one, in Slack or email,
so you can change your mind until the main run (about 2 hours before the deadline), which then
closes the Slack buttons. A bare "yes" when there are several decisions is unclear and approves
nothing; the main-run message says so.

Only emails from your own address (GMAIL_ADDRESS or EMAIL_TO) carrying the preview's code
(e.g. fpl-3f9a1c) count; the main run reads them over IMAP with the Gmail app password. In Slack,
the Worker takes taps only from you (SLACK_USER_ID), and the main run reads the answers back from
the message (channels:history).

python3 approvals.py --slack-check   tests the Slack buttons end to end (slack_check).
"""
import email
import imaplib
import json
import os
import re
import secrets
import ssl
import sys
from datetime import datetime
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import doc
import slack

STATE = Path(__file__).with_name("state") / "proposal.json"
BOT_SUBJECT = "FPL: "                    # the bot's own emails (mailer.SUBJECT_PREFIX): never answers
WORDS = {"yes": "yes", "y": "yes", "ok": "yes", "okay": "yes", "approve": "yes", "approved": "yes", "go": "yes",
         "no": "no", "n": "no", "nope": "no", "decline": "no", "skip": "no"}
NUMBERED = re.compile(r"#?\b(\d{1,2})\b\s*[:.)=\-]?\s*(yes|y|ok|okay|approve|approved|go|no|n|nope|decline|skip)\b", re.I)
PLAIN = re.compile(r"\b(yes|ok|okay|approve|approved|no|nope|decline|skip)\b", re.I)
REVERSED = re.compile(r"\b(yes|ok|okay|approve|approved|no|nope|decline|skip)\b\s*(?:to|for|on)?\s*#?(\d{1,2})\b", re.I)
BUTTON = re.compile(r"#(\d{1,2})\s+(YES|NO)\b")
RECORD = re.compile(r"^answer-([a-z0-9.]+)-([a-z0-9]+)-(\d+)$")   # worker.js writes one for each tap: key, choice, time
TEST_CODE = "fpl-test"


def new_code():
    return "fpl-" + secrets.token_hex(3)


def save(proposal):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(proposal, indent=1))


def load(gw):
    """The proposal waiting for this gameweek, if any."""
    if not STATE.exists():
        return None
    proposal = json.loads(STATE.read_text())
    return proposal if proposal.get("gw") == gw else None


def button_link(code, number, answer):
    """mailto: link for a Yes/No button: an email to the bot that says exactly that."""
    to = os.getenv("GMAIL_ADDRESS", "")
    subject = f"FPL decision {code} #{number} {answer.upper()}"
    return f"mailto:{to}?subject={quote(subject)}&body={quote(f'{number} {answer}')}"


def own_lines(message):
    """What you typed in a reply, above any quoted text."""
    part = next((p for p in message.walk() if p.get_content_type() == "text/plain"), None)
    if part is None:
        return []
    text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
    raw = [line.strip() for line in text.splitlines()]
    lines = []
    for k, line in enumerate(raw):
        following = raw[k + 1] if k + 1 < len(raw) else ""
        quoted = (line.startswith((">", "-----Original", "From: ", "________________"))
                  or (line.startswith("On ") and "wrote:" in f"{line} {following}"))
        if quoted:
            break
        if line:
            lines.append(line)
    return lines[:10]


def answers_in(message, count):
    """{decision number: 'yes'/'no'} from one email, and a note if it couldn't be read."""
    found = {int(n): "yes" if a.upper() == "YES" else "no" for n, a in BUTTON.findall(message.get("Subject", ""))}
    lines = own_lines(message)
    for line in lines:
        pairs = NUMBERED.findall(line) or [(n, w) for w, n in REVERSED.findall(line)]
        for number, word in pairs:
            if 1 <= int(number) <= count:
                found[int(number)] = WORDS[word.lower()]
    if found:
        return found, None
    plain = [WORDS[w.lower()] for line in lines for w in PLAIN.findall(line)]
    if not plain:
        return {}, None
    if count == 1:
        return {1: plain[0]}, None
    if len(plain) == count:
        return dict(enumerate(plain, start=1)), None
    return {}, (f"Your reply \"{' '.join(lines)[:80]}\" didn't say which decision it meant, so it counted for "
                f"nothing. Answer each one: \"1 yes\", \"2 no\".")


def verdicts(messages, code, owners, count, times=None):
    """{decision number: 'yes'/'no'} from all replies (oldest first, so later ones win), plus notes.
    times, if given, gets each answer's time (unix seconds, from the email's Date)."""
    answers, notes = {}, []
    for message in messages:
        subject = message.get("Subject", "")
        sender = parseaddr(message.get("From", ""))[1].lower()
        if code not in subject or subject.startswith(BOT_SUBJECT) or sender not in owners:
            continue
        found, note = answers_in(message, count)
        answers.update(found)
        if times is not None:
            try:
                sent = parsedate_to_datetime(message.get("Date", "")).timestamp()
            except (TypeError, ValueError):
                sent = 0
            times.update({n: sent for n in found})
        if note:
            notes.append(note)
    return answers, notes


def check(code, since, count, times=None):
    """Read your answers from the inbox: ({decision number: 'yes'/'no'}, notes); times as in verdicts."""
    address = os.getenv("GMAIL_ADDRESS", "")
    password = (os.getenv("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    owners = {a.lower() for a in (address, os.getenv("EMAIL_TO", "")) if a}
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()
    with imaplib.IMAP4_SSL("imap.gmail.com", ssl_context=context) as imap:
        imap.login(address, password)
        imap.select("INBOX", readonly=True)
        _, found = imap.search(None, "SINCE", since.strftime("%d-%b-%Y"), "SUBJECT", code)
        messages = []
        for number in found[0].split():
            _, parts = imap.fetch(number, "(RFC822)")
            messages.append(email.message_from_bytes(parts[0][1]))
    return verdicts(messages, code, owners, count, times)


# ---------------------------------------------------------------- Slack

def buttons(code, number):
    """The Yes and No for one decision: a real button in Slack (worker.js), a ready-made email in the email."""
    return [doc.Button("Yes", button_link(code, number, "yes"), "good",
                       plain=f"   reply \"{number} yes\" or \"{number} no\"", action=f"{code}|{number}|yes"),
            doc.Button("No", button_link(code, number, "no"), "bad", action=f"{code}|{number}|no")]


def slack_message(ts):
    """A message in SLACK_CHANNEL as Slack holds it now, with the answers worker.js wrote into it."""
    found = slack.call("conversations.history", {"channel": os.environ["SLACK_CHANNEL"], "latest": ts, "oldest": ts,
                                                 "inclusive": "true", "limit": 1}, form=True)["messages"]
    if not found:
        raise LookupError(f"the preview's Slack message ({ts}) is gone")
    return found[0]


def records(message):
    """{key: (choice, unix time)} from the blocks worker.js (or overrides.py) wrote into a message: a
    decision's number and yes/no, or an override such as keep.123.gw13 and on/off."""
    found = {}
    for block in message.get("blocks", []):
        match = RECORD.match(block.get("block_id", ""))
        if match:
            found[match[1]] = (match[2], int(match[3]))
    return found


def slack_answers(message):
    """{decision number: ('yes'/'no', unix time)} from the answers worker.js wrote into the message."""
    return {int(key): found for key, found in records(message).items()
            if key.isdigit() and len(key) <= 2 and found[0] in ("yes", "no")}


def values(block):
    """What an actions block's buttons and menus send when used."""
    out = []
    for element in block.get("elements", []) if block.get("type") == "actions" else []:
        out += [element["value"]] if "value" in element else [o.get("value", "") for o in element.get("options", [])]
    return out


def answers(proposal):
    """Your answers to the proposal's decisions, by email and in Slack; for each decision the later
    answer counts. Returns ({decision number: 'yes'/'no'}, notes, the Slack message or None)."""
    count = len(proposal["decisions"])
    found, times, notes, failed = {}, {}, [], []
    try:
        found, unclear = check(proposal["code"], datetime.fromisoformat(proposal["created"]), count, times)
        notes += unclear
    except Exception as problem:
        failed.append(f"email: {type(problem).__name__}: {problem}")
    message = None
    if proposal.get("slack_ts") and slack.configured():
        try:
            message = slack_message(proposal["slack_ts"])
            for number, (answer, at) in slack_answers(message).items():
                if 1 <= number <= count and at >= times.get(number, 0):
                    found[number] = answer
        except Exception as problem:
            failed.append(f"Slack: {type(problem).__name__}: {problem}")
    sources = 1 + bool(proposal.get("slack_ts") and slack.configured())
    if failed and len(failed) == sources:
        notes.append(f"Couldn't read your answers ({'; '.join(failed)}), so every decision was treated as no.")
    elif failed:
        notes.append(f"Couldn't read your answers by {failed[0]}; your other answers counted.")
    return found, notes, message


def close_slack(proposal, message, note):
    """Swaps the preview's Yes/No buttons for a note (e.g. "🔒 Answers closed at the main run, 11:13 ET."),
    keeping your answers: a re-run of the main run reads them again."""
    code, blocks = proposal["code"], []
    for block in message.get("blocks", []):
        sends = values(block)
        if sends and all(v.startswith(code + "|") for v in sends):
            blocks.append(slack.context(note))
        else:
            blocks.append(block)
    slack.call("chat.update", {"channel": os.environ["SLACK_CHANNEL"], "ts": proposal["slack_ts"], "blocks": blocks,
                               "text": message.get("text", "")})


def slack_check():
    """Tests the buttons end to end, without a real decision (workflow job slack-check). The first run
    posts a test decision with Yes and No; tap one, then run it again: it reads your answer back the
    way the main run does, says what it read in the test's thread, and closes the test."""
    history = slack.call("conversations.history", {"channel": os.environ["SLACK_CHANNEL"], "limit": 30},
                         form=True)["messages"]
    test = next((m for m in history for b in m.get("blocks", [])
                 if any(v.startswith(TEST_CODE + "|") for v in values(b))), None)
    if test is None:
        message = doc.Message("Slack buttons check", blocks=[doc.Callout("1 test decision", [
            [doc.Tone("1. ", bold=True), "A test: nothing happens either way"], buttons(TEST_CODE, 1),
            doc.Tone("Tap Yes or No: your answer should appear under the buttons. Then run slack-check again to "
                     "read it back.", "muted")], "warn")])
        ts = slack.post(message.title, doc.to_slack(message)[0]["layouts"][0])
        print(f"Posted the test decision (ts {ts}). Tap Yes or No in Slack, then run slack-check again.")
        return True
    proposal = {"code": TEST_CODE, "slack_ts": test["ts"]}
    read = slack_answers(test).get(1)
    said = (f"you said {read[0]} at {datetime.fromtimestamp(read[1]).strftime('%H:%M')}" if read
            else "no answer yet: did your tap show under the buttons? If not, check the Worker (README, Slack)")
    close_slack(proposal, test, "🔒 Test closed.")
    slack.post(f"Read back from Slack: {said}.", thread_ts=test["ts"])
    print(f"Read back: {said}. Test closed.")
    return bool(read)


if __name__ == "__main__":
    if sys.argv[1:] == ["--slack-check"]:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).with_name(".env"))
        sys.exit(0 if slack_check() else 1)
    sys.exit("Usage: python3 approvals.py --slack-check")
