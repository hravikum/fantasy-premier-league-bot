"""Sends the bot's messages: to Slack (slack.py) once SLACK_BOT_TOKEN and SLACK_CHANNEL are set, and
by email through Gmail with an app password (same setup as the ESPN bot).

Email goes too when Slack isn't set up, when the Slack post fails, while EMAIL_TOO is "true" (the
trial of Slack beside email), and always for a message that needs you (urgent: ACTION NEEDED and
the hosted_run.sh alerts), which in Slack also @-mentions you when SLACK_USER_ID is set.
"""
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

import certifi
from dotenv import load_dotenv

import slack

SUBJECT_PREFIX = "FPL: "


def send(subject, body, html=None, slack_posts=None, urgent=False):
    """Send one message. Returns True if it reached you, by Slack or by email; never raises.

    slack_posts is doc.to_slack's layout (the message, then replies in its thread); without it the
    message goes to Slack as the subject over the body in monospace.
    """
    load_dotenv(Path(__file__).with_name(".env"))
    posted = slack.configured() and _post(subject, body, slack_posts, urgent)
    emailed = False
    if urgent or not posted or os.getenv("EMAIL_TOO", "").strip().lower() == "true":
        emailed = _email(subject, body, html)
    if not (posted or emailed):
        print(f'(nothing delivered: "{subject}" reached neither Slack nor email)')
    return posted or emailed


def _post(subject, body, posts, urgent):
    """The message in the channel, then its replies in the thread. True once the message is in:
    a lost reply only loses detail, which the email and the run's log still have."""
    posts = posts or [{"text": subject, "layouts": [slack.plain(subject, body)]}]
    first = posts[0]["layouts"]
    text = subject
    who = slack.mention() if urgent else ""
    if who:
        text = f"{who} {subject}"
        first = [layout[:1] + [slack.section(who)] + layout[1:] for layout in first]
    try:
        ts = slack.post_first(text, first)
    except Exception as problem:   # a dead notifier must not kill a run that already acted
        print(f"(Slack post failed: {type(problem).__name__}: {problem})")
        return False
    for reply in posts[1:]:
        try:
            slack.post_first(reply["text"], reply["layouts"], thread_ts=ts)
        except Exception as problem:
            print(f"(Slack thread reply failed: {type(problem).__name__}: {problem})")
    return True


def _email(subject, body, html):
    address = os.getenv("GMAIL_ADDRESS")
    password = (os.getenv("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    if not (address and password):
        print("(no email sent: set GMAIL_ADDRESS and GMAIL_APP_PASSWORD)")
        return False
    message = EmailMessage()
    message["From"] = address
    message["To"] = os.getenv("EMAIL_TO") or address
    message["Subject"] = SUBJECT_PREFIX + subject
    message.set_content(body, cte="quoted-printable")
    if html:
        message.add_alternative(html, subtype="html", cte="quoted-printable")
    try:
        context = ssl.create_default_context(cafile=certifi.where())
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
            smtp.starttls(context=context)
            smtp.login(address, password)
            smtp.send_message(message)
        return True
    except Exception as problem:   # a dead notifier must not kill a run
        print(f"(email failed: {type(problem).__name__}: {problem})")
        return False
