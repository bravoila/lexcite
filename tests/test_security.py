import logging
import stat
from fastapi.testclient import TestClient
from app import db
from app.main import app


# ---------- P1-1 会话 Cookie Secure ----------

def test_cookie_secure_when_public_url_https(client, monkeypatch):
    monkeypatch.setenv("LEXCITE_PUBLIC_URL", "https://legal.example.com")
    r = client.post("/api/login", json={"password": "test-only-password-123"})
    assert r.status_code == 200
    assert "Secure" in r.headers["set-cookie"]


def test_cookie_secure_when_flag_set(client, monkeypatch):
    monkeypatch.setenv("LEXCITE_SECURE_COOKIE", "1")
    r = client.post("/api/login", json={"password": "test-only-password-123"})
    assert r.status_code == 200
    assert "Secure" in r.headers["set-cookie"]


def test_cookie_not_secure_by_default(client, monkeypatch):
    monkeypatch.delenv("LEXCITE_PUBLIC_URL", raising=False)
    monkeypatch.delenv("LEXCITE_SECURE_COOKIE", raising=False)
    r = client.post("/api/login", json={"password": "test-only-password-123"})
    assert r.status_code == 200
    assert "Secure" not in r.headers["set-cookie"]


def test_startup_warns_when_cookie_not_secure(client, monkeypatch, caplog):
    monkeypatch.delenv("LEXCITE_PUBLIC_URL", raising=False)
    monkeypatch.delenv("LEXCITE_SECURE_COOKIE", raising=False)
    with caplog.at_level(logging.WARNING, logger="lexcite"):
        with TestClient(app, headers={"X-Lexcite": "1"}):
            pass
    assert any("未启用 Secure" in m for m in caplog.messages)


# ---------- P1-2 服务地址 SSRF 校验 ----------

def _settings_payload(base_url):
    return {"profile": "local", "provider": "none", "base_url": base_url, "model": "qwen3:8b",
            "timeout": 120, "max_tokens": 4000, "review_days": 180, "thinking_mode": "auto",
            "show_reasoning": True, "json_mode": "auto", "online_research": False,
            "agent_rounds": 3, "agent_timeout": 360}


def test_endpoint_blocks_private_loopback_reserved(client):
    for bad in ("http://127.0.0.1:8000/v1", "http://[::1]:8000/v1", "http://169.254.169.254/latest",
                "http://10.0.0.5/v1", "http://192.168.1.5/v1", "http://0.0.0.0:8000/v1",
                "http://metadata.google.internal/v1"):
        r = client.post("/api/settings", json=_settings_payload(bad))
        assert r.status_code == 422 and "不允许" in r.text, bad


def test_endpoint_rejects_malformed_urls(client):
    for bad in ("http://user:pass@evil.example.com/v1", "http://evil.example.com/v1?x=1",
                "http://evil.example.com/v1#f", "ftp://evil.example.com/v1"):
        r = client.post("/api/settings", json=_settings_payload(bad))
        assert r.status_code == 422 and "格式不正确" in r.text, bad


def test_endpoint_allows_named_local_hosts_and_public(client):
    for good in ("http://localhost:11434/v1", "http://host.docker.internal:11434/v1",
                 "http://ollama:11434/v1", "https://api.deepseek.com"):
        r = client.post("/api/settings", json=_settings_payload(good))
        assert r.status_code == 200, good


def test_retrieval_settings_endpoint_blocked(client):
    r = client.post("/api/retrieval/settings",
                    json={"embedding": {"provider": "none", "base_url": "http://127.0.0.1:8080"},
                          "reranker": {"provider": "none", "base_url": "http://localhost:8081"}})
    assert r.status_code == 422 and "不允许" in r.text
    r = client.post("/api/retrieval/settings",
                    json={"embedding": {"provider": "none", "base_url": "http://ollama:11434"},
                          "reranker": {"provider": "none", "base_url": "http://localhost:8081"}})
    assert r.status_code == 200


# ---------- P1-3 鉴权先于 body 读取 ----------

def test_unauthenticated_large_post_rejected_before_body(client):
    unauth = TestClient(app, headers={"X-Lexcite": "1"})  # 未登录：只带请求校验标记
    body = {"title": "t", "event_date": "2026-01-01", "pad": "A" * 9_000_000}
    r = unauth.post("/api/cases", json=body)
    assert r.status_code == 401  # 在读取 9MB body 之前即拒绝，而非 413


def test_authenticated_oversize_post_413(client):
    body = {"title": "t", "event_date": "2026-01-01", "pad": "A" * 9_000_000}
    r = client.post("/api/cases", json=body)
    assert r.status_code == 413 and "8MB" in r.text


# ---------- P1-4 数据目录与文件权限 ----------

def test_data_files_restricted_permissions(client):
    d = db.data_dir()
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE((d / "lexcite.sqlite3").stat().st_mode) == 0o600
    for suffix in ("-wal", "-shm"):
        p = d / f"lexcite.sqlite3{suffix}"
        if p.exists():
            assert stat.S_IMODE(p.stat().st_mode) == 0o600
    key = d / "secret.key"
    if key.exists():
        assert stat.S_IMODE(key.stat().st_mode) == 0o600


# ---------- P1-5 初始化码不落 stdout ----------

def test_setup_code_not_leaked_to_stdout(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LEXCITE_DATA", str(tmp_path))
    monkeypatch.delenv("LEXCITE_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("MODEL_PROFILE", "local")
    monkeypatch.setenv("MODEL_LOCAL_PROVIDER", "none")
    with TestClient(app, headers={"X-Lexcite": "1"}):
        pass
    out = capsys.readouterr().out
    code = (db.data_dir() / "setup-code.txt").read_text()
    assert "setup-code.txt" in out
    assert code not in out


# ---------- P1-6 请求来源白名单 ----------

def _case_body():
    return {"title": "来源校验测试", "event_date": "2026-01-01", "jurisdiction": "全国"}


def test_origin_mismatch_rejected_when_public_url_set(client, monkeypatch):
    monkeypatch.setenv("LEXCITE_PUBLIC_URL", "https://legal.example.com")
    r = client.post("/api/cases", json=_case_body(), headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403 and "请求来源不匹配" in r.text
    r = client.post("/api/cases", json=_case_body(), headers={"Origin": "https://legal.example.com"})
    assert r.status_code == 200


def test_origin_ok_without_public_url(client):
    r = client.post("/api/cases", json=_case_body(), headers={"Origin": "http://testserver"})
    assert r.status_code == 200
