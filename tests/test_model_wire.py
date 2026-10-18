"""Real HTTP protocol test with a deterministic stub, not a real LLM evaluation."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import pytest
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
            elif "candidate_evidence" in payload:
                content={"selected_ids":[x["id"] for x in payload["candidate_evidence"]],"coverage":"sufficient","reason":"HTTP fixture only"}
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
        llm.save_settings({"provider":"custom","base_url":f"http://127.0.0.1:{server.server_port}/v1","model":"mock-only","api_key":""})
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
