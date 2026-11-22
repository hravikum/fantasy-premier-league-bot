"""Asking you first: each hit or chip is its own numbered decision in the preview email.

Answer each one, either way:
  - tap its Yes or No button: it opens a ready-made email to the bot, you press Send
  - or reply to the preview with one answer per decision: "1 yes" / "2 no" (any order, any lines),
    or just the answers in the order listed ("yes, no"). With a single decision, "yes" is enough.
A decision you don't answer is skipped. A later answer replaces an earlier one, so you can change
your mind until the main run (about 2 hours before the deadline). A bare "yes" when there are
several decisions is unclear and approves nothing; the main-run email says so.

Only emails from your own address (GMAIL_ADDRESS or EMAIL_TO) carrying the preview's code
(e.g. fpl-3f9a1c) count. The main run reads them over IMAP with the Gmail app password.
"""
import email
import imaplib
import json
import os
import re
import secrets
import ssl
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import quote

STATE = Path(__file__).with_name("state") / "proposal.json"
BOT_SUBJECT = "FPL: "                    # the bot's own emails (mailer.SUBJECT_PREFIX): never answers
WORDS = {"yes": "yes", "y": "yes", "ok": "yes", "okay": "yes", "approve": "yes", "approved": "yes", "go": "yes",
         "no": "no", "n": "no", "nope": "no", "decline": "no", "skip": "no"}
NUMBERED = re.compile(r"#?\b(\d{1,2})\b\s*[:.)=\-]?\s*(yes|y|ok|okay|approve|approved|go|no|n|nope|decline|skip)\b", re.I)
PLAIN = re.compile(r"\b(yes|ok|okay|approve|approved|no|nope|decline|skip)\b", re.I)
REVERSED = re.compile(r"\b(yes|ok|okay|approve|approved|no|nope|decline|skip)\b\s*(?:to|for|on)?\s*#?(\d{1,2})\b", re.I)
BUTTON = re.compile(r"#(\d{1,2})\s+(YES|NO)\b")


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


def verdicts(messages, code, owners, count):
    """{decision number: 'yes'/'no'} from all replies (oldest first, so later ones win), plus notes."""
    answers, notes = {}, []
    for message in messages:
        subject = message.get("Subject", "")
        sender = parseaddr(message.get("From", ""))[1].lower()
        if code not in subject or subject.startswith(BOT_SUBJECT) or sender not in owners:
            continue
        found, note = answers_in(message, count)
        answers.update(found)
        if note:
            notes.append(note)
    return answers, notes


def check(code, since, count):
    """Read your answers from the inbox: ({decision number: 'yes'/'no'}, notes)."""
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
    return verdicts(messages, code, owners, count)
