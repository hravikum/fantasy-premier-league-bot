"""Login tests with a fake FPL token endpoint.  python3 test_auth.py  (or pytest)"""
import tempfile
from pathlib import Path

import requests

import auth


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(self.status_code)


class FakeServer:
    """Hands out rotating refresh tokens; any token can be used once."""

    def __init__(self, valid):
        self.valid, self.count, self.seen = {valid}, 0, []

    def get(self, url, **kwargs):
        return FakeResponse(200, {"token_endpoint": "https://account.premierleague.com/as/token"})

    def post(self, url, data=None, **kwargs):
        self.seen.append(data)
        assert data["grant_type"] == "refresh_token" and data["client_id"] == auth.CLIENT_ID and "scope" not in data
        if data["refresh_token"] not in self.valid:
            return FakeResponse(400, {"error": "invalid_grant", "error_description": "token revoked"})
        self.valid.discard(data["refresh_token"])
        self.count += 1
        new = f"rt-{self.count}"
        self.valid.add(new)
        return FakeResponse(200, {"access_token": f"at-{self.count}", "refresh_token": new, "expires_in": 3600})


def setup(monkeypatch, server, seed="seed-1", key="a long random key"):
    store = Path(tempfile.mkdtemp()) / "token.enc"
    monkeypatch.setattr(auth, "STORE", store)
    monkeypatch.setattr(requests, "Session", lambda: server)
    monkeypatch.setenv("FPL_STATE_KEY", key)
    monkeypatch.setenv("FPL_REFRESH_TOKEN", seed)
    return store


def test_rotates_and_reuses_the_saved_token(monkeypatch):
    server = FakeServer("seed-1")
    store = setup(monkeypatch, server)
    assert auth.access_token() == "at-1"
    assert b"rt-1" not in store.read_bytes()                       # stored encrypted
    assert auth.access_token() == "at-2"                           # used rt-1, not the spent seed
    assert [d["refresh_token"] for d in server.seen] == ["seed-1", "rt-1"]


def test_a_new_sign_in_replaces_the_chain(monkeypatch):
    server = FakeServer("seed-1")
    setup(monkeypatch, server)
    auth.access_token()
    server.valid.add("seed-2")
    monkeypatch.setenv("FPL_REFRESH_TOKEN", "seed-2")
    assert auth.access_token() == "at-2"
    assert server.seen[-1]["refresh_token"] == "seed-2"


def test_revoked_login_says_what_to_do(monkeypatch):
    setup(monkeypatch, FakeServer("something-else"))
    try:
        auth.access_token()
        raise AssertionError("expected LoginNeeded")
    except auth.LoginNeeded as problem:
        assert "token revoked" in str(problem) and "private window" in str(problem)


def test_wrong_key_or_no_login(monkeypatch):
    server = FakeServer("seed-1")
    setup(monkeypatch, server)
    auth.access_token()
    monkeypatch.setenv("FPL_STATE_KEY", "a different key")
    try:
        auth.access_token()
        raise AssertionError("expected LoginNeeded")
    except auth.LoginNeeded as problem:
        assert "FPL_STATE_KEY" in str(problem)
    setup(monkeypatch, FakeServer("x"), seed="")
    try:
        auth.access_token()
        raise AssertionError("expected LoginNeeded")
    except auth.LoginNeeded as problem:
        assert "No FPL login yet" in str(problem)
