"""Real HTTP protocol test with a deterministic stub, not a real LLM evaluation."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import pytest
import httpx
from app import llm


@pytest.mark.parametrize('repair_quote',[False,True])
def test_full_http_model_roundtrip(client,corpus,repair_quote):
    captured=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_POST(self):
            assert self.path == "/v1/chat/completions"
            body=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append(body)
            payload=json.loads(body["messages"][1]["content"])
            if "failed_claims" in payload:
                failed=payload["failed_claims"][0]
                claim=failed["claim"]
                a=next(x for x in payload["evidence"] if x["id"]==claim["evidence"][0]["article_id"])
                claim["evidence"][0]["quote"]=a["body"]
                content={"patches":[{"claim_index":failed["claim_index"],"claim":claim}]}
            elif "draft" in payload:
                content={"checks":[{"claim_index":i,"supported":True,"reason":"HTTP fixture only"} for i in range(len(payload["draft"]))]}
            elif "evidence" in payload:
                a=next(x for x in payload["evidence"] if x["number"]=="第五百七十九条")
                content={"insufficient":False,"claims":[{"text":"在未支付价款的情形下，对方可以请求支付。","kind":"rule","evidence":[{"article_id":a["id"],"quote":"不存在的合成测试原文" if repair_quote else a["body"]}]}],"missing_facts":[]}
            else:
                content={"queries":["未支付价款 请求支付"]}
            data=json.dumps({"choices":[{"finish_reason":"stop","message":{"content":json.dumps(content,ensure_ascii=False)}}],"usage":{"prompt_tokens":100,"completion_tokens":40,"total_tokens":140}}).encode()
            self.send_response(200);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        llm.save_settings({"provider":"custom","base_url":f"http://localhost:{server.server_port}/v1","model":"mock-only","api_key":""})
        cid=client.post("/api/cases",json={"title":"HTTP test","event_date":"2025-01-01"}).json()["id"]
        response=client.post(f"/api/cases/{cid}/ask",json={"text":"对方未支付合同价款可以请求其支付吗？"})
        assert response.status_code==200,response.text
        result=response.json()
        assert result["status"]=="answered" and len(captured)==(3 if repair_quote else 2)
        assert [run['stage'] for run in result['model_runs']]==(['法律分析','局部修正','证据复核'] if repair_quote else ['法律分析','证据复核'])
        assert all(run['usage']['total_tokens']==140 and run['elapsed_seconds']>=0 and run['payload_characters']>0 for run in result['model_runs'])
        assert result["citations"][0]["number"]=="第五百七十九条"
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)


def test_endpoint_change_drops_stored_key(client):
    from app import db
    llm.save_settings({"profile":"api","provider":"custom","base_url":"https://first.example/v1","model":"m","api_key":"sk-old-secret"})
    assert llm.resolved()["api_key"]=="sk-old-secret"
    llm.save_settings({"profile":"api","provider":"custom","base_url":"https://second.example/v1","model":"m"})
    assert llm.resolved()["api_key"]==""
    assert "sk-old-secret" not in json.dumps(db.get("settings"))


def test_unchanged_endpoint_keeps_stored_key(client):
    llm.save_settings({"profile":"api","provider":"custom","base_url":"https://first.example/v1","model":"m","api_key":"sk-old-secret"})
    llm.save_settings({"profile":"api","provider":"custom","base_url":"https://first.example/v1","model":"m2"})
    assert llm.resolved()["api_key"]=="sk-old-secret"


def test_endpoint_change_never_inherits_environment_key(client, monkeypatch):
    monkeypatch.setenv("MODEL_CLOUD_PROVIDER", "custom")
    monkeypatch.setenv("MODEL_CLOUD_BASE_URL", "https://configured.example/v1")
    monkeypatch.setenv("MODEL_CLOUD_API_KEY", "environment-secret")
    llm.save_settings({"profile": "api", "provider": "custom", "base_url": "https://configured.example/v1"})
    assert llm.resolved()["api_key"] == "environment-secret"
    llm.save_settings({"profile": "api", "provider": "custom", "base_url": "https://changed.example/v1"})
    assert llm.resolved()["api_key"] == ""
    assert not llm.public_settings()["environment_api_key"]


def test_explicit_empty_key_disables_environment_fallback(client, monkeypatch):
    monkeypatch.setenv("MODEL_CLOUD_API_KEY", "environment-secret")
    llm.save_settings({"profile": "api", "provider": "dashscope", "base_url": llm.profile_defaults("api")["base_url"], "api_key": ""})
    assert llm.resolved()["api_key"] == ""


def test_legacy_inferred_profile_keeps_endpoint_bound_key(client):
    from app import db
    encrypted = llm.cipher().encrypt(b"legacy-secret").decode()
    db.put("settings", {"provider": "dashscope", "base_url": "https://first.example/v1", "api_key_encrypted": encrypted})
    llm.save_settings({"profile": "api", "provider": "dashscope", "base_url": "https://first.example/v1"})
    assert llm.resolved()["api_key"] == "legacy-secret"


def _mock_adapter(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(llm.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))


def _reply():
    return {"choices": [{"finish_reason": "stop", "message": {"content": '{"queries":["ok"]}'}}]}


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_http_errors_are_retried_with_bounded_attempts(client, monkeypatch, status):
    from app.research_agent.schemas import Plan
    llm.save_settings({"provider": "custom", "base_url": "http://localhost:9999", "model": "test"})
    calls, attempts = [], []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Retry-After": "0"}, json={"error": {"message": "temporary"}}) if len(calls) < 3 else httpx.Response(200, json=_reply())
    _mock_adapter(monkeypatch, handler)
    with llm.capture_attempts(attempts.append):
        assert llm.request_json("test", {}, Plan).queries == ["ok"]
    assert attempts == [1, 2, 3] and len(calls) == 3


def test_retry_after_larger_than_remaining_budget_does_not_send_more_requests(client, monkeypatch):
    from app.research_agent.schemas import Plan
    llm.save_settings({"provider": "custom", "base_url": "http://localhost:9999", "model": "test", "timeout": 10})
    calls = []
    _mock_adapter(monkeypatch, lambda request: calls.append(request) or httpx.Response(429, headers={"Retry-After": "60"}, json={}))
    with pytest.raises(llm.ModelError, match="剩余时限"):
        llm.request_json("test", {}, Plan)
    assert len(calls) == 1


def test_retry_after_http_date_is_parsed():
    from email.utils import formatdate
    import time
    response = httpx.Response(503, headers={"Retry-After": formatdate(time.time()+30, usegmt=True)}, json={})
    with pytest.raises(llm.ModelError) as error:
        llm.check_status(response)
    assert 28 <= error.value.retry_after <= 30


def test_backoff_checks_cancellation_without_waiting_full_delay():
    checks = []
    def cancel():
        checks.append(True)
        raise InterruptedError("cancelled")
    with pytest.raises(InterruptedError):
        llm.wait_before_retry(60, cancel)
    assert checks == [True]


@pytest.mark.parametrize("streaming", [False, True])
def test_all_response_modes_enforce_byte_limit(client, monkeypatch, streaming):
    from app.research_agent.schemas import Plan
    llm.save_settings({"provider": "custom", "base_url": "http://localhost:9999", "model": "test", "show_reasoning": streaming})
    class Oversize(httpx.SyncByteStream):
        def __iter__(self):
            yield b"x" * 2_000_001
            yield b"x" * 2_000_001
    _mock_adapter(monkeypatch, lambda request: httpx.Response(200, headers={"Content-Type": "application/json"}, stream=Oversize()))
    with llm.capture_reasoning(lambda _: None), pytest.raises(llm.ModelError, match="大小限制"):
        llm.request_json("test", {}, Plan)


def test_newline_free_sse_bytes_still_check_cancellation(client, monkeypatch):
    from app.research_agent.schemas import Plan
    llm.save_settings({"provider": "custom", "base_url": "http://localhost:9999", "model": "test"})
    count = []
    class Dripping(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(100):
                count.append(True)
                yield b"x"
    def check():
        if len(count) >= 3:
            raise InterruptedError("cancelled during unframed data")
    _mock_adapter(monkeypatch, lambda request: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=Dripping()))
    with llm.controlled(check, llm.time.monotonic()+10), llm.capture_reasoning(lambda _: None), pytest.raises(InterruptedError):
        llm.request_json("test", {}, Plan)
    assert len(count) == 3


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
def test_stalled_response_is_interrupted_without_waiting_for_next_byte(client, streaming, cancel):
    import time
    from app.research_agent.schemas import Plan
    release, cancelled = threading.Event(), threading.Event()
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(True)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if streaming else "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"1\r\n{\r\n")
            self.wfile.flush()
            release.wait(3)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    timer = threading.Timer(.2, cancelled.set) if cancel else None
    def check():
        if cancelled.is_set():
            raise InterruptedError("synthetic cancellation")
    try:
        llm.save_settings({"provider": "custom", "base_url": f"http://localhost:{server.server_port}/v1", "model": "fixture", "show_reasoning": streaming})
        started = time.monotonic()
        if timer:
            timer.start()
        with llm.controlled(check, started+(5 if cancel else .25)), llm.capture_reasoning(lambda _: None):
            with pytest.raises(InterruptedError if cancel else llm.ModelError):
                llm.request_json("test", {}, Plan)
        assert time.monotonic()-started < 1.2
        assert requests == [True]
    finally:
        release.set()
        if timer:
            timer.cancel()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
