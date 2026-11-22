"""Email through Gmail with an app password (same setup as the ESPN bot)."""
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

import certifi
from dotenv import load_dotenv

SUBJECT_PREFIX = "FPL: "


def send(subject, body, html=None):
    """Send one email: plain text, plus an HTML version when given. Returns True if it went out;
    never raises."""
    load_dotenv(Path(__file__).with_name(".env"))
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
