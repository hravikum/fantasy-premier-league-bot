"""Log in to FPL without a password: keep a refresh token and rotate it on every run.

FPL's login moved to PingOne (account.premierleague.com) for 2026/27. The site signs in with OAuth
in the browser and calls the API with `X-API-Authorization: Bearer <access token>`; scripted
password logins are refused. So you sign in once in a private browser window and copy the
refresh token (README, "Log in once"). Every refresh hands back a NEW refresh token and retires
the old one, so the new one is saved, encrypted, before anything else happens.

  FPL_REFRESH_TOKEN  the token you copied. A new value starts a new chain (after a fresh sign-in).
  FPL_STATE_KEY      any long random string; encrypts state/token.enc (kept on the bot-state branch).
"""
import base64
import hashlib
import json
import os
import time
from pathlib import Path

import requests
from cryptography.fernet import Fernet, InvalidToken

ISSUER = "https://account.premierleague.com/as"
CLIENT_ID = "bfcbaf69-aade-4c1b-8f00-c1cb8a193030"     # the FPL website's public OAuth client
STORE = Path(__file__).with_name("state") / "token.enc"
SIGN_IN_AGAIN = ("Sign in again in a private window, copy the new token into the FPL_REFRESH_TOKEN "
                 "secret (README: 'Log in once'), and the next run picks it up.")


class LoginNeeded(Exception):
    """The bot can't log in until you do something; the message says what."""


def _fernet():
    key = os.getenv("FPL_STATE_KEY", "")
    if not key:
        raise LoginNeeded("FPL_STATE_KEY is not set.")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))


def _fingerprint(token):
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _load():
    if not STORE.exists():
        return {}
    try:
        return json.loads(_fernet().decrypt(STORE.read_bytes()))
    except InvalidToken:
        raise LoginNeeded("state/token.enc can't be decrypted: was FPL_STATE_KEY changed? " + SIGN_IN_AGAIN)


def _save(record):
    STORE.parent.mkdir(exist_ok=True)
    temp = STORE.with_suffix(".tmp")
    temp.write_bytes(_fernet().encrypt(json.dumps(record).encode()))
    temp.replace(STORE)


def _token_endpoint(session):
    try:
        return session.get(ISSUER + "/.well-known/openid-configuration", timeout=20).json()["token_endpoint"]
    except (requests.RequestException, ValueError, KeyError):
        return ISSUER + "/token"


def access_token():
    """A fresh access token. The refresh token is rotated and saved on the way."""
    seed = os.getenv("FPL_REFRESH_TOKEN", "").strip()
    record = _load()
    if seed and record.get("seed") != _fingerprint(seed):
        record = {"refresh_token": seed, "seed": _fingerprint(seed)}      # a new sign-in starts a new chain
    if not record.get("refresh_token"):
        raise LoginNeeded("No FPL login yet. " + SIGN_IN_AGAIN)
    session = requests.Session()
    response = session.post(_token_endpoint(session), timeout=30, data={
        "grant_type": "refresh_token", "refresh_token": record["refresh_token"], "client_id": CLIENT_ID})
    if response.status_code in (400, 401):
        try:
            reason = response.json().get("error_description") or response.json().get("error")
        except ValueError:
            reason = f"HTTP {response.status_code}, not JSON (blocked?)"
        raise LoginNeeded(f"FPL refused the saved login ({reason}). " + SIGN_IN_AGAIN)
    response.raise_for_status()
    body = response.json()
    record.update(refresh_token=body.get("refresh_token", record["refresh_token"]), rotated_at=int(time.time()))
    _save(record)
    return body["access_token"]
