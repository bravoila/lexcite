import json
from app import db, llm
from app.research_agent import runner as engine
from app.research_agent.schemas import CoverageAudit, FactExtraction, Draft, Plan, Verdict


def new_case(c,title="合同纠纷"):
    return c.post("/api/cases",json={"title":title,"event_date":"2025-01-01","jurisdiction":"全国"}).json()["id"]


def test_offline_flow_memory_export_persistence(client,corpus):
    a,b=new_case(client),new_case(client,"另一个案件")
    mid=client.post(f"/api/cases/{a}/memories",json={"text":"甲尚未支付价款"}).json()["id"]
    assert client.get(f"/api/cases/{b}").json()["memories"] == []
    result=client.post(f"/api/cases/{a}/ask",json={"text":"未支付合同价款应该如何处理？","mode":"extract"}).json()
    assert result["status"]=="evidence_only" and result["citations"]
    assert len(client.get(f"/api/cases/{a}").json()["messages"])==2
    db.init()
    assert len(client.get(f"/api/cases/{a}").json()["memories"])==1
    assert "SHA256" in client.get(f"/api/cases/{a}/export").text
    client.delete(f"/api/cases/{a}/memories/{mid}")
    assert client.get(f"/api/cases/{a}").json()["memories"]==[]
    client.delete(f"/api/cases/{a}")
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM messages WHERE case_id=?",(a,)).fetchone()[0]==0


def test_no_corpus_refuses(client):
    cid=new_case(client)
    result=client.post(f"/api/cases/{cid}/ask",json={"text":"应如何承担责任？"}).json()
    assert result["status"]=="insufficient" and not result["claims"]


def set_model():
    llm.save_settings({"provider":"custom","base_url":"http://localhost:9999/v1","model":"test-only","api_key":"secret-test"})


def test_combined_analysis_review_flow_and_recall(client,corpus,monkeypatch):
    set_model();cid=new_case(client)
    client.post(f"/api/cases/{cid}/memories",json={"text":"付款期限已经届满"})
    calls=[]
    def fake(system,payload,schema,verify=False):
        calls.append(payload)
        if schema is CoverageAudit:return CoverageAudit(selected_ids=[x['id'] for x in payload['candidate_evidence']],coverage='sufficient',reason='Synthetic protocol fixture; not a legal assessment')
        if schema is FactExtraction:return FactExtraction(intent='supplement',facts=[])
        if schema is Plan:return Plan(queries=["未支付价款"])
        if schema is Draft:
            row=next(e for e in payload["evidence"] if e["number"]=="第五百七十九条")
            return Draft(insufficient=False,claims=[{"text":"对于未支付价款的情形，对方可以请求支付。","kind":"rule","evidence":[{"article_id":row["id"],"quote":row["body"]}]}])
        return Verdict(checks=[{"claim_index":0,"supported":True,"reason":"matches"}])
    monkeypatch.setattr(llm,"request_json",fake)
    r=client.post(f"/api/cases/{cid}/ask",json={"text":"对方未支付价款怎么办？"}).json()
    assert r["status"]=="answered" and len(r["claims"])==1 and len(calls)==2
    assert calls[0]["confirmed_facts"]==["付款期限已经届满"]
    assert r["citations"][0]["number"]=="第五百七十九条"
    assert r["verification"]["legal_correctness_guaranteed"] is False
    client.post(f"/api/cases/{cid}/ask",json={"text":"如果他仍不支付呢？"})
    followup=next(payload for payload in calls[2:] if 'evidence' in payload)
    assert followup["previous_user_messages_unconfirmed"]==["对方未支付价款怎么办？"]


def test_unsupported_or_unavailable_verification_blocks(client,corpus,monkeypatch):
    set_model();cid=new_case(client)
    def fake(system,payload,schema,verify=False):
        if schema is CoverageAudit:return CoverageAudit(selected_ids=[x['id'] for x in payload['candidate_evidence']],coverage='sufficient',reason='Synthetic protocol fixture; not a legal assessment')
        if schema is FactExtraction:return FactExtraction(intent='supplement',facts=[])
        if schema is Plan:return Plan(queries=[])
        if schema is Draft:
            a=payload["evidence"][0]
            return Draft(insufficient=False,claims=[{"text":"对方一定要赔偿十万元。","kind":"inference","evidence":[{"article_id":a["id"],"quote":a["body"]}]}])
        return Verdict(checks=[{"claim_index":0,"supported":False,"reason":"金额没有依据"}])
    monkeypatch.setattr(llm,"request_json",fake)
    result=client.post(f"/api/cases/{cid}/ask",json={"text":"未支付价款怎么处理"}).json()
    assert result["status"]=="no_supported_answer" and result["claims"]==[]
    def broken(*args,**kwargs):raise llm.ModelError("offline")
    monkeypatch.setattr(llm,"request_json",broken)
    result=client.post(f"/api/cases/{cid}/ask",json={"text":"未支付价款怎么处理"}).json()
    assert result["status"]=="model_error" and result["claims"]==[]


def test_fabricated_id_never_rendered(client,corpus,monkeypatch):
    set_model();cid=new_case(client)
    def fake(system,payload,schema,verify=False):
        if schema is CoverageAudit:return CoverageAudit(selected_ids=[x['id'] for x in payload['candidate_evidence']],coverage='sufficient',reason='Synthetic protocol fixture; not a legal assessment')
        if schema is FactExtraction:return FactExtraction(intent='supplement',facts=[])
        if schema is Plan:return Plan(queries=[])
        return Draft(insufficient=False,claims=[{"text":"虚假规则","kind":"rule","evidence":[{"article_id":"does-not-exist","quote":"这个不存在的规则会被阻止"}]}])
    monkeypatch.setattr(llm,"request_json",fake)
    r=client.post(f"/api/cases/{cid}/ask",json={"text":"未支付价款怎么处理"}).json()
    assert r["claims"]==[] and "虚假规则" not in r["summary"]


def test_password_csrf_key_not_exposed(client):
    set_model()
    value=client.get("/api/settings").json()
    assert value["has_api_key"] and "secret-test" not in json.dumps(value)
    assert "api_key_encrypted" not in value
    with db.connect() as c:
        assert "secret-test" not in c.execute("SELECT value FROM kv WHERE key='settings'").fetchone()[0]
    assert client.post("/api/cases",json={"title":"x"},headers={"X-Lexcite":""}).status_code==403
    assert client.post("/api/cases",json={"title":"x"},headers={"Origin":"https://evil.example"}).status_code==403
    client.post("/api/logout")
    assert client.get("/api/cases").status_code==401


def test_nonofficial_source_rejected(client):
    value=json.loads((db.ROOT/"examples/civil_excerpt.json").read_text())
    value["source_url"]="https://example.com/fake-law"
    assert client.post("/api/laws",json=value).status_code==422


def test_api_adapter_headers_schema(client,monkeypatch):
    set_model()
    import httpx
    captured=[]
    class FakeClient:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,url,json,headers):
            captured.append((url,json,headers))
            return httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"content":'{"queries":["测试"]}'}}]})
    monkeypatch.setattr(llm.httpx,"Client",FakeClient)
    assert llm.request_json("test",{},Plan).queries==["测试"]
    url,body,headers=captured[0]
    assert url.endswith("/v1/chat/completions") and body["response_format"]=={"type":"json_object"}
    assert headers["Authorization"]=="Bearer secret-test"
