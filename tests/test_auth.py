import time
import pytest
from app import auth

WRONG = "wrong-password-999"
RIGHT = "test-only-password-123"


@pytest.fixture(autouse=True)
def _reset_limits():
    auth.ATTEMPTS.clear()
    auth.GLOBAL_FAILS[:] = []
    yield
    auth.ATTEMPTS.clear()
    auth.GLOBAL_FAILS[:] = []


def _login(client, address, password):
    # Behind a trusted proxy the app uses the last X-Forwarded-For hop.
    return client.post("/api/login", json={"password": password},
                       headers={"X-Forwarded-For": f"9.9.9.9, {address}"})


def test_attacker_flood_does_not_block_other_clients(client, monkeypatch):
    monkeypatch.setenv("LEXCITE_TRUST_PROXY", "1")
    for _ in range(30):
        assert _login(client, "203.0.113.9", WRONG).status_code in (401, 429)
    assert _login(client, "198.51.100.7", RIGHT).status_code == 200


def test_account_limit_still_locks_flooded_address(client, monkeypatch):
    monkeypatch.setenv("LEXCITE_TRUST_PROXY", "1")
    for _ in range(10):
        assert _login(client, "203.0.113.10", WRONG).status_code == 401
    assert _login(client, "203.0.113.10", RIGHT).status_code == 429


def test_forged_xff_ignored_without_trusted_proxy(client):
    # LEXCITE_TRUST_PROXY unset: XFF is spoofable input and must not change
    # the rate-limit key; all requests share the proxy's own address.
    for _ in range(10):
        assert _login(client, "203.0.113.11", WRONG).status_code == 401
    assert _login(client, "203.0.113.11", RIGHT).status_code == 429


def test_loopback_recovery_channel(client, monkeypatch):
    monkeypatch.setenv("LEXCITE_TRUST_PROXY", "1")
    for _ in range(10):
        assert _login(client, "203.0.113.12", WRONG).status_code == 401
    assert auth.login(RIGHT, "127.0.0.1")


def test_global_anonymous_cap_throttles_but_never_locks(client, monkeypatch):
    monkeypatch.setenv("LEXCITE_TRUST_PROXY", "1")
    for i in range(60):
        assert _login(client, f"203.0.113.{20 + i % 40}", WRONG).status_code == 401
    started = time.monotonic()
    r = _login(client, "198.51.100.99", RIGHT)
    assert r.status_code == 200 and time.monotonic() - started >= 1.9
