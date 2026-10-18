"""Import regressions: provenance, review gating, URL boundaries and old databases."""
import hashlib
import io
import json
import socket
import sqlite3
import time

import pytest
from app import db, sources
from app.research_agent import retrieval


def example():
    return json.loads((db.ROOT / "examples/civil_excerpt.json").read_text())


def test_file_without_url_review_citation_and_export(client):
    law = example()
    law.pop("source_url")
    raw = law["text"].encode()
    extracted = client.post("/api/extract", files={"file": ("法规原文.txt", raw, "text/plain")})
    assert extracted.status_code == 200
    law.update({k: extracted.json()[k] for k in ("text", "source_name", "source_kind")})
    assert client.post("/api/laws/preview", json=law).json()["count"] == 3
    result = client.post("/api/laws", json=law)
    assert result.status_code == 200, result.text
    lid = result.json()["id"]
    saved = client.get(f"/api/laws/{lid}").json()["law"]
    assert saved["source_url"] == "" and saved["source_name"] == "法规原文.txt"
    assert saved["sha256"] == hashlib.sha256(raw).hexdigest()
    assert not retrieval.corpus("2025-01-01", "全国")[0]
    client.post(f"/api/laws/{lid}/review", json={"confirmed": True, "valid_from": "2021-01-01"})
    cid = client.post("/api/cases", json={"title": "文件引用", "event_date": "2025-01-01"}).json()["id"]
    answer = client.post(f"/api/cases/{cid}/ask", json={"text": "民法典第577条", "mode": "extract"}).json()
    assert answer["citations"]
    assert answer["citations"][0]["source_kind"] == "file"
    assert answer["citations"][0]["source_name"] == "法规原文.txt"
    assert answer["citations"][0]["source_url"] == ""
    exported = client.get(f"/api/cases/{cid}/export").text
    assert "法规原文.txt" in exported and "未提供；由导入人核对来源" in exported
    db.init()
    assert client.get(f"/api/cases/{cid}").json()["messages"][1]["payload"]["citations"] == answer["citations"]


def test_paste_without_url_and_legacy_payload(client):
    law = example()
    law.pop("source_url")
    assert client.post("/api/laws", json=law).status_code == 200
    law["version"] = "有来源链接的旧接口格式"
    law["source_url"] = example()["source_url"]
    assert client.post("/api/laws", json=law).status_code == 200
    law["source_kind"] = "url"
    law["source_url"] = ""
    assert client.post("/api/laws", json=law).status_code == 422


def test_bad_file_and_scanned_pdf_errors(client):
    from pypdf import PdfWriter
    stream = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(stream)
    for name, content, message in [("scan.pdf", stream.getvalue(), "OCR"),
                                   ("empty.txt", b"   ", "未从文件"),
                                   ("law.exe", b"test", "支持 TXT")]:
        r = client.post("/api/extract", files={"file": (name, content)})
        assert r.status_code == 400 and message in r.json()["detail"]


def test_docx_without_url(client):
    from docx import Document
    doc = Document()
    doc.add_paragraph("第一条 本条只是导入流程测试，不是法律依据。")
    stream = io.BytesIO()
    doc.save(stream)
    r = client.post("/api/extract", files={"file": ("测试.docx", stream.getvalue())})
    assert r.status_code == 200
    law = example() | {"text": r.json()["text"], "source_url": "", "source_kind": "file", "source_name": r.json()["source_name"]}
    assert client.post("/api/laws/preview", json=law).json()["count"] == 1


@pytest.mark.parametrize("url", ["https://example.com/law", "http://www.gov.cn/law",
    "https://www.gov.cn.evil.example/law", "https://user@www.gov.cn/law",
    "https://www.gov.cn:8443/law", "https://127.0.0.1/law", "https://www.gov.cn/\r\nx"])
def test_url_rejects_unsafe_input_before_fetch(client, monkeypatch, url):
    def no_network(*args):
        pytest.fail("invalid URL reached the network")
    monkeypatch.setattr(sources.socket, "getaddrinfo", no_network)
    assert client.post("/api/extract-url", json={"url": url}).status_code == 422


def public_dns(*args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))]


def test_url_only_html_import_with_checked_redirect(client, monkeypatch):
    monkeypatch.setattr(sources.socket, "getaddrinfo", public_dns)
    calls = []
    def fetch(url, address, deadline):
        calls.append((url, address))
        if len(calls) == 1:
            return 302, {"location": "https://www.npc.gov.cn/final.html"}, b""
        body = "<html><head><script>bad()</script></head><body><nav>导航</nav>" + "".join("<p>"+p+"</p>" for p in example()["text"].splitlines()) + "<footer>版权页尾</footer></body></html>"
        return 200, {"content-type": "text/html; charset=utf-8"}, body.encode()
    monkeypatch.setattr(sources, "_fetch_once", fetch)
    r = client.post("/api/extract-url", json={"url": "https://www.gov.cn/start"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["count"] == 3 and d["source_url"] == "https://www.npc.gov.cn/final.html"
    assert "版权页尾" not in d["text"] and "bad()" not in d["text"]
    assert calls == [("https://www.gov.cn/start", "8.8.8.8"), (d["source_url"], "8.8.8.8")]
    law = example() | {k: d[k] for k in ("text", "source_url", "source_kind", "source_name")}
    r = client.post("/api/laws", json=law)
    assert r.status_code == 200
    assert client.get(f"/api/laws/{r.json()['id']}").json()["law"]["reviewed_at"] is None


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.2", "169.254.169.254", "::1"])
def test_official_name_cannot_resolve_to_private_address(client, monkeypatch, address):
    monkeypatch.setattr(sources.socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (address, 443))])
    monkeypatch.setattr(sources, "_fetch_once", lambda *a: pytest.fail("private IP connected"))
    r = client.post("/api/extract-url", json={"url": "https://www.gov.cn/law"})
    assert r.status_code == 400 and "非公网" in r.json()["detail"]


def test_redirect_domain_and_hop_limits(client, monkeypatch):
    monkeypatch.setattr(sources.socket, "getaddrinfo", public_dns)
    calls = []
    def fetch(*args):
        calls.append(args[0])
        return 302, {"location": "https://example.com/law"}, b""
    monkeypatch.setattr(sources, "_fetch_once", fetch)
    assert client.post("/api/extract-url", json={"url": "https://www.gov.cn/law"}).status_code == 400
    assert len(calls) == 1
    monkeypatch.setattr(sources, "_fetch_once", lambda *a: (302, {"location": "/loop"}, b""))
    r = client.post("/api/extract-url", json={"url": "https://www.gov.cn/law"})
    assert r.status_code == 400 and "跳转次数" in r.json()["detail"]


@pytest.mark.parametrize("status,headers,body,expected", [
    (200, {"content-type": "text/html"}, b"<p>Please enable JavaScript</p>", "未能从链接"),
    (403, {}, b"", "HTTP 403"),
    (200, {"content-type": "text/plain;charset=gb2312"}, "第一条 本条用于测试文本的解码和导入。".encode("gb18030"), None),
])
def test_url_read_failure_or_gb_text(client, monkeypatch, status, headers, body, expected):
    monkeypatch.setattr(sources.socket, "getaddrinfo", public_dns)
    monkeypatch.setattr(sources, "_fetch_once", lambda *a: (status, headers, body))
    r = client.post("/api/extract-url", json={"url": "https://www.gov.cn/law.txt"})
    assert r.status_code == (400 if expected else 200)
    if expected:
        assert expected in r.json()["detail"]
    else:
        assert r.json()["count"] == 1
    assert client.get("/api/laws").json() == []  # reading never silently imports/enables


def test_download_bound_without_content_length(monkeypatch):
    class FakeResponse:
        status = 200
        def getheaders(self):
            return []
        def read1(self, size):
            return b"x" * size
    closed = []
    class FakeConnection:
        sock = None
        def __init__(self, *args, **kwargs): pass
        def request(self, *args, **kwargs): pass
        def getresponse(self): return FakeResponse()
        def close(self): closed.append(True)
    monkeypatch.setattr(sources, "PinnedHTTPSConnection", FakeConnection)
    with pytest.raises(ValueError, match="超过8MB"):
        sources._fetch_once("https://www.gov.cn/law", "8.8.8.8", time.monotonic()+10)
    assert closed


def test_pinned_connection_uses_original_hostname_for_tls(monkeypatch):
    calls = []
    class FakeContext:
        def wrap_socket(self, raw, server_hostname):
            calls.append(("tls", server_hostname))
            return raw
    monkeypatch.setattr(sources.ssl, "create_default_context", lambda: FakeContext())
    monkeypatch.setattr(sources.socket, "create_connection", lambda address, timeout: calls.append(("tcp", address)) or object())
    c = sources.PinnedHTTPSConnection("www.gov.cn", "8.8.8.8", 10)
    c.connect()
    assert calls == [("tcp", ("8.8.8.8", 443)), ("tls", "www.gov.cn")]


def test_upgrade_100_database_keeps_existing_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("LEXCITE_DATA", str(tmp_path))
    # The exact 1.0.0 laws schema, before provenance columns existed.
    with sqlite3.connect(tmp_path / "lexcite.sqlite3") as c:
        c.execute("""CREATE TABLE laws(id TEXT PRIMARY KEY,name TEXT NOT NULL,version TEXT NOT NULL,
          jurisdiction TEXT NOT NULL,kind TEXT NOT NULL,source_url TEXT NOT NULL,valid_from TEXT NOT NULL,
          valid_to TEXT,reviewed_at TEXT,sha256 TEXT NOT NULL,created TEXT NOT NULL,UNIQUE(name,version,jurisdiction))""")
        c.execute("INSERT INTO laws VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("old-id", "旧法规", "版本一", "全国", "法律", "https://www.gov.cn/old", "2021-01-01", None, "2026-09-24", "old-hash", "2026-09-24"))
    db.init()
    db.put("settings", {"provider": "none", "model": "unchanged"})
    db.init()  # restarting is safe
    with db.connect() as c:
        row = dict(c.execute("SELECT * FROM laws").fetchone())
    assert row["id"] == "old-id" and row["source_url"] == "https://www.gov.cn/old"
    assert row["reviewed_at"] == "2026-09-24" and row["sha256"] == "old-hash"
    assert row["source_name"] == "导入正文"
    assert db.get("settings")["model"] == "unchanged"


def test_legacy_saved_citation_export(client, corpus):
    cid = client.post("/api/cases", json={"title": "旧记录", "event_date": "2025-01-01"}).json()["id"]
    answer = client.post(f"/api/cases/{cid}/ask", json={"text": "民法典第577条", "mode": "extract"}).json()
    for citation in answer["citations"]:
        citation.pop("source_name")
        citation.pop("source_kind")
    with db.connect() as c:
        c.execute("UPDATE messages SET payload=? WHERE case_id=? AND role='assistant'", (json.dumps(answer), cid))
    r = client.get(f"/api/cases/{cid}/export")
    assert r.status_code == 200 and "导入正文" in r.text and "https://" in r.text
