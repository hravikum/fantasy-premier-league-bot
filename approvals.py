"""Asking you first: hits and chips wait for a reply to the preview email.

The preview email's subject carries a code like fpl-3f9a1c. Reply to it with YES (anything starting
yes / ok / approve) and the main run does what it asked; reply NO, or don't reply, and it doesn't.
Only a reply from your own address (GMAIL_ADDRESS or EMAIL_TO) counts. The main run reads your
inbox over IMAP with the same Gmail app password the emails go out with, looking only for that code.
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

STATE = Path(__file__).with_name("state") / "proposal.json"
YES = re.compile(r"^(yes|y|ok|okay|approve|approved|go|do it)\b", re.I)
NO = re.compile(r"^(no|n|nope|decline|skip|don't|dont)\b", re.I)


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


def first_line(message):
    """The first line you typed in a reply, above any quoted text."""
    part = next((p for p in message.walk() if p.get_content_type() == "text/plain"), None)
    if part is None:
        return ""
    text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(">") or re.match(r"^On .+ wrote:$", line):
            break
        if line:
            return line
    return ""


def verdict(messages, code, owners):
    """'yes', 'no' or None from replies (newest last) that carry the code and come from you."""
    answer = None
    for message in messages:
        subject = message.get("Subject", "")
        sender = parseaddr(message.get("From", ""))[1].lower()
        if code not in subject or not subject.lower().startswith("re:") or sender not in owners:
            continue
        line = first_line(message)
        if YES.match(line):
            answer = "yes"
        elif NO.match(line):
            answer = "no"
    return answer


def check(code, since):
    """Look in the inbox for your reply. Returns 'yes', 'no' or None."""
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
    return verdict(messages, code, owners)
