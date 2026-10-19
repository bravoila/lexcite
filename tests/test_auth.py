import time
import pytest
from fastapi import HTTPException
from app import auth

WRONG = "wrong-password-999"
RIGHT = "test-only-password-123"


@pytest.fixture(autouse=True)
def _reset_limits():
    auth.ATTEMPTS.clear()
    auth.GLOBAL_FAILS[:] = []
    auth._global_next = 0
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


def test_source_limit_rejects_failures_but_accepts_correct_password(client, monkeypatch):
    monkeypatch.setenv("LEXCITE_TRUST_PROXY", "1")
    for _ in range(10):
        assert _login(client, "203.0.113.10", WRONG).status_code == 401
    response = _login(client, "203.0.113.10", WRONG)
    assert response.status_code == 429 and int(response.headers["retry-after"]) > 0
    assert _login(client, "203.0.113.10", RIGHT).status_code == 200


def test_forged_xff_ignored_without_trusted_proxy(client, monkeypatch):
    # LEXCITE_TRUST_PROXY unset: XFF is spoofable input and must not change
    # the rate-limit key; all requests share the proxy's own address.
    monkeypatch.setenv("LEXCITE_TRUST_PROXY", "0")
    for i in range(10):
        assert _login(client, f"203.0.113.{i}", WRONG).status_code == 401
    assert _login(client, "198.51.100.99", WRONG).status_code == 429
    assert list(auth.ATTEMPTS) == ["testclient"]
    assert _login(client, "198.51.100.99", RIGHT).status_code == 200


def test_loopback_is_limited_by_default_and_recovery_is_explicit(client, monkeypatch):
    monkeypatch.setenv("LEXCITE_LOCAL_RECOVERY", "0")
    for _ in range(10):
        with pytest.raises(HTTPException) as error:
            auth.login(WRONG, "127.0.0.1")
        assert error.value.status_code == 401
    with pytest.raises(HTTPException) as error:
        auth.login(WRONG, "127.0.0.1")
    assert error.value.status_code == 429
    auth.GLOBAL_FAILS[:] = [time.time()] * 60
    monkeypatch.setenv("LEXCITE_LOCAL_RECOVERY", "1")
    with pytest.raises(HTTPException) as error:
        auth.login(WRONG, "127.0.0.1", local_recovery=True)
    assert error.value.status_code == 401
    assert auth.login(RIGHT, "127.0.0.1", local_recovery=True)


def test_forwarded_loopback_never_gets_recovery_exemption(client, monkeypatch):
    monkeypatch.setenv("LEXCITE_TRUST_PROXY", "1")
    monkeypatch.setenv("LEXCITE_LOCAL_RECOVERY", "1")
    for _ in range(10):
        assert _login(client, "127.0.0.1", WRONG).status_code == 401
    assert _login(client, "127.0.0.1", WRONG).status_code == 429


def test_global_anonymous_cap_is_nonblocking_and_never_locks_correct_password(client, monkeypatch):
    monkeypatch.setenv("LEXCITE_TRUST_PROXY", "1")
    for i in range(60):
        assert _login(client, f"203.0.113.{20 + i % 40}", WRONG).status_code == 401
    _login(client, "198.51.100.98", WRONG)
    response = _login(client, "198.51.100.97", WRONG)
    assert response.status_code == 429 and response.headers["retry-after"] == "2"
    monkeypatch.setattr(auth.time, "sleep", lambda _: pytest.fail("login must not sleep"))
    started = time.monotonic()
    r = _login(client, "198.51.100.99", RIGHT)
    assert r.status_code == 200 and time.monotonic() - started < 1


def test_attempt_addresses_are_bounded_and_expired_entries_removed(client, monkeypatch):
    monkeypatch.setattr(auth, "MAX_ATTEMPT_ADDRESSES", 3)
    for i in range(5):
        with pytest.raises(HTTPException):
            auth.login(WRONG, f"203.0.113.{i}")
    assert len(auth.ATTEMPTS) == 3
    auth.ATTEMPTS["expired"] = [time.time() - 601]
    assert auth.login(RIGHT, "198.51.100.1")
    assert "expired" not in auth.ATTEMPTS


def test_cold_password_cache_does_not_lock_out_correct_password(client, monkeypatch):
    monkeypatch.setattr(auth, "_verified_password", None)
    auth.ATTEMPTS["shared-proxy"] = [time.time()] * 10
    auth.GLOBAL_FAILS[:] = [time.time()] * 60
    assert auth.login(RIGHT, "shared-proxy")
