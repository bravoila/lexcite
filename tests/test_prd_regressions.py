"""Deterministic 1.4.0 regressions; synthetic text is not legal-quality evidence."""
from contextlib import contextmanager
import json
import threading
import time

import httpx
import pytest
from pydantic import ValidationError

from app import db, indexing, jobs, llm, retrieval_services, sources
from app.research_agent import authorities, context, retrieval, runner
from app.research_agent.schemas import AuthorityNeed, Claim, Draft, Plan, Repair, Verdict
from app.research_agent.verification import validate_claim


def law(client, name="民法典", numbers=(3, 5, 264, 577), body="合成测试条文，仅供回归测试，不是真实法律。"):
    response = client.post("/api/laws", json={
        "name": name, "version": "synthetic", "valid_from": "2021-01-01",
        "text": "\n".join(f"第{n}条 {body}" for n in numbers),
    })
    assert response.status_code == 200, response.text
    lid = response.json()["id"]
    assert client.post("/api/laws/"+lid+"/review", json={"confirmed": True, "valid_from": "2021-01-01"}).status_code == 200
    return lid


def case(client):
    return client.post("/api/cases", json={"title": "1.4.0 regression", "event_date": "2025-01-01"}).json()["id"]


def model_settings(**overrides):
    llm.save_settings({"provider": "custom", "base_url": "http://localhost:9999/v1", "model": "synthetic", "agent_rounds": 1} | overrides)


@pytest.mark.parametrize("query", ["刑法:第264条", "(刑法)第264条", "（刑法）第264条", "解释(一)第3条", "《未知规范》264条"])
def test_foreign_subject_with_punctuation_cannot_match_single_law(client, query):
    law(client)
    hits, warnings = retrieval.search([query], "2025-01-01", "全国")
    assert hits == []
    assert any("未入库法规" in warning for warning in warnings)


@pytest.mark.parametrize("query", ["刑法第264条和民法典第577条", "《民法典》577条与《刑法》264条", "(刑法)第264条，民法典：第577条"])
@pytest.mark.parametrize("criminal_imported", [False, True])
def test_each_article_binds_its_nearest_law(client, query, criminal_imported):
    civil = law(client)
    criminal = law(client, "刑法", (264, 577)) if criminal_imported else None
    hits, warnings = retrieval.search([query], "2025-01-01", "全国")
    exact = {(row["law_id"], row["number"]) for row in hits if "exact" in row.get("retrieval_channels", [])}
    expected = {(civil, "第577条")}
    if criminal:
        expected.add((criminal, "第264条"))
    assert exact == expected
    assert not any(row["law_id"] == civil and row["number"] == "第264条" for row in hits)
    assert not any(row["law_id"] == criminal and row["number"] == "第577条" for row in hits)
    assert any("未入库法规" in warning for warning in warnings) != criminal_imported


@pytest.mark.parametrize("query", ["根据民法典第577条", "本法典第577条", "相关司法解释第5条", "合同规定第5条"])
def test_query_prefixes_and_referents_are_not_foreign_law_names(client, query):
    law(client)
    hits, warnings = retrieval.search([query], "2025-01-01", "全国")
    assert hits
    assert not any("未入库" in warning for warning in warnings)


def test_numbered_interpretation_identity_and_multiple_articles(client):
    lid = law(client, "测试解释（一）", (3, 5))
    hits, warnings = retrieval.search(["测试解释(一)：第3条、第5条"], "2025-01-01", "全国")
    assert {(row["law_id"], row["number"]) for row in hits if "exact" in row["retrieval_channels"]} == {(lid, "第3条"), (lid, "第5条")}
    assert not any("未入库法规" in warning for warning in warnings)


@pytest.mark.parametrize("exclusion", ["date", "review", "jurisdiction"])
def test_catalogued_but_excluded_law_is_not_reported_as_unimported(client, exclusion):
    lid = law(client)
    with db.connect() as connection:
        if exclusion == "date":
            connection.execute("UPDATE laws SET valid_from='2030-01-01' WHERE id=?", (lid,))
        elif exclusion == "review":
            connection.execute("UPDATE laws SET reviewed_at=NULL WHERE id=?", (lid,))
        else:
            connection.execute("UPDATE laws SET jurisdiction='上海市' WHERE id=?", (lid,))
    hits, warnings = retrieval.search(["民法典第577条"], "2025-01-01", "全国")
    assert hits == []
    assert any("已入库但" in warning for warning in warnings)
    assert not any("未入库法规" in warning for warning in warnings)


@pytest.mark.parametrize("text", ["(刑法)第264条规定测试责任。", "刑法：第264条规定测试责任。", "《刑法》264条规定测试责任。"])
def test_claim_validation_uses_the_same_subject_binding(client, text):
    law(client)
    rows, _ = retrieval.corpus("2025-01-01", "全国")
    row = next(row for row in rows if row["number"] == "第264条")
    claim = Claim(text=text, kind="rule", evidence=[{"article_id": row["id"], "quote": row["body"]}])
    assert not validate_claim(claim, rows)[0]
    claim.text = "根据民法典第264条规定测试责任。"
    assert validate_claim(claim, rows)[0]


@pytest.mark.parametrize("number", ["第0条", "第一条之零", "第1条之0"])
def test_invalid_model_article_numbers_are_rejected_at_their_boundaries(client, number):
    law(client, numbers=(1,))
    rows, _ = retrieval.corpus("2025-01-01", "全国")
    claim = Claim(text=number+"规定合成责任。", kind="rule", evidence=[{"article_id": rows[0]["id"], "quote": rows[0]["body"]}])
    valid, reason = validate_claim(claim, rows)
    assert not valid and "无效条号" in reason
    with pytest.raises(ValidationError):
        AuthorityNeed(name="测试法规", articles=[number])


def test_corpus_and_lexical_cache_invalidate_on_direct_database_edits(client):
    lid = law(client, numbers=(1,))
    first, _ = retrieval.search(["民法典第1条"], "2025-01-01", "全国")
    version = db.corpus_version()
    replacement = "新的合成测试内容，修改后必须进入缓存与引用，不能复用旧正文。"
    with db.connect() as connection:
        connection.execute("UPDATE articles SET body=? WHERE law_id=?", (replacement, lid))
    second, _ = retrieval.search(["民法典第1条"], "2025-01-01", "全国")
    assert db.corpus_version() != version
    assert second[0]["body"] == replacement != first[0]["body"]
    with db.connect() as connection:
        connection.execute("UPDATE article_headings SET heading_path='第一章 新章名'")
    assert retrieval.corpus("2025-01-01", "全国")[0][0]["heading_path"] == "第一章 新章名"
    with db.connect() as connection:
        connection.execute("DELETE FROM laws WHERE id=?", (lid,))
    assert retrieval.corpus("2025-01-01", "全国")[0] == []


def test_repeated_retrieval_reuses_filtered_corpus_and_lexical_statistics(client, monkeypatch):
    law(client)
    statements = []
    original = db.connect
    @contextmanager
    def tracked():
        with original() as connection:
            connection.set_trace_callback(statements.append)
            yield connection
    monkeypatch.setattr(db, "connect", tracked)
    retrieval.lexical_index.cache_clear()
    retrieval.search(["民法典第577条"], "2025-01-01", "全国")
    info = retrieval.lexical_index.cache_info()
    statements.clear()
    retrieval.search(["民法典第264条"], "2025-01-01", "全国")
    retrieval.authority_gaps([{"name": "民法典", "articles": ["第577条"]}], "2025-01-01", "全国")
    retrieval.requested_articles([{"name": "民法典", "articles": ["第264条"]}], "2025-01-01", "全国")
    assert retrieval.lexical_index.cache_info().hits == info.hits+1
    assert not any("FROM articles" in sql or "FROM laws" in sql for sql in statements)
    assert retrieval._corpus.cache_info().currsize <= 4


def test_five_thousand_article_retrieval_under_two_seconds(client):
    lid = law(client, numbers=(1,))
    text = "合成性能测试正文，只用于验证本地检索延迟。义务人应当按约履行义务，具体法律效果必须另行核对。" * 2
    with db.connect() as connection:
        connection.execute("DELETE FROM articles WHERE law_id=?", (lid,))
        connection.executemany("INSERT INTO articles VALUES(?,?,?,?,?)", [
            (f"performance-{i}", lid, f"第{i}条", text, i) for i in range(1, 5001)
        ])
    retrieval.lexical_index.cache_clear()
    started = time.perf_counter()
    hits, _ = retrieval.search(["民法典第4999条"], "2025-01-01", "全国")
    elapsed = time.perf_counter()-started
    assert any(row["number"] == "第4999条" and "exact" in row["retrieval_channels"] for row in hits)
    assert elapsed < 2, f"5000-article cold retrieval took {elapsed:.3f}s"


@pytest.mark.parametrize("instruction", ["请分析", "详细解释合同效力", "给出结论", "继续分析", "请根据以上事实解释"])
def test_question_followed_by_instruction_is_not_a_fact(client, instruction):
    cid = case(client)
    text = "合同是否有效？"+instruction+"。"
    assert context.question_answer_pairs(text) == []
    state = context.load(cid, text, [])
    assert not any(fact.get("answer") or instruction in fact["text"] for fact in state["facts"])
    assert "合同是否有效" in state["research_focus"]


def test_current_new_request_is_not_replaced_by_old_statements(client):
    cid = case(client)
    db.add_message(cid, "user", {"text": "甲去年购买一辆汽车。"})
    state = context.load(cid, "分析今年发生的房屋租赁纠纷", [])
    assert state["research_focus"] == "分析今年发生的房屋租赁纠纷"
    assert not any("分析今年" in fact["text"] for fact in state["facts"])


@pytest.mark.parametrize("wrong_stage", [Plan(queries=["test"]), Verdict(checks=[]), Repair(patches=[])])
def test_draft_rejects_other_stage_json(wrong_stage):
    with pytest.raises(ValidationError):
        Draft.model_validate(wrong_stage.model_dump())
    assert Draft.model_validate({}) == Draft()


def test_model_gap_for_available_article_is_not_a_server_source_gap(client, corpus, monkeypatch):
    model_settings()
    cid = case(client)
    calls = []
    def model(system, payload, schema, verify=False):
        calls.append(schema)
        assert schema is Draft
        return Draft(missing_authorities=[{"name": corpus["law"]["name"], "articles": ["第579条"]}])
    monkeypatch.setattr(llm, "request_json", model)
    monkeypatch.setattr(authorities.ResearchSources, "acquire", lambda *args: pytest.fail("available local article must not trigger acquisition"))
    value = runner.answer(cid, "未支付价款怎么办？", online_research=True)
    assert value["source_gaps"] == [] and calls == [Draft]


def test_long_requested_provision_is_explicitly_omitted_or_returned_complete(client, monkeypatch):
    model_settings()
    monkeypatch.setattr(llm, "request_json", lambda *args, **kwargs: Draft())
    body = "这是一段合成超长条文，仅供上下文预算测试，不是实际法律依据。" * 700
    law(client, numbers=(1,), body=body)
    cid = case(client)
    strict = runner.answer(cid, "民法典第1条", online_research=False)
    assert not strict["citations"]
    assert strict["evidence_omissions"][0]["number"] == "第1条"
    assert any("18000" in warning and "未截断" in warning for warning in strict["warnings"])
    raw = runner.answer(cid, "民法典第1条", mode="extract")
    assert raw["status"] == "evidence_only" and not raw["evidence_omissions"]
    assert raw["citations"][0]["body"] == body


def test_omission_warning_is_removed_if_a_later_round_includes_the_provision(client, monkeypatch):
    model_settings(agent_rounds=2)
    law(client, numbers=(1, 2), body="合成预算测试文本，不是真实法律依据。" * 700)
    rows, _ = retrieval.corpus("2025-01-01", "全国")
    searches, reviews = [], []
    def search(*args):
        searches.append(True)
        ordered = rows if len(searches) == 1 else list(reversed(rows))
        return [row | {"requested": True, "retrieval_channels": ["exact"]} for row in ordered], []
    def model(system, payload, schema, verify=False):
        if schema is Plan:
            return Plan(queries=["调整合成资料顺序"])
        if schema is Draft:
            row = payload["evidence"][0]
            return Draft(insufficient=False, claims=[{"text": "合成测试结论，不是真实法律意见。", "kind": "rule", "evidence": [{"article_id": row["id"], "quote": row["body"][:80]}]}])
        assert schema is Verdict
        reviews.append(True)
        return Verdict(checks=[{"claim_index": 0, "supported": len(reviews) > 1, "action": "retrieve", "reason": "合成回归"}])
    monkeypatch.setattr(retrieval, "search", search)
    monkeypatch.setattr(llm, "request_json", model)
    value = runner.answer(case(client), "请比较两段测试资料。", online_research=False)
    assert len(searches) == 2 and value["status"] == "answered"
    assert [row["id"] for row in value["citations"]] == [rows[1]["id"]]
    assert [row["id"] for row in value["evidence_omissions"]] == [rows[0]["id"]]
    assert any(rows[0]["number"] in warning and "18000" in warning for warning in value["warnings"])
    assert not any(rows[1]["number"] in warning and "18000" in warning for warning in value["warnings"])


def test_failed_source_read_has_skipped_timing_not_success(client, monkeypatch):
    model_settings()
    cid = case(client)
    need = {"name": authorities.CATALOG[0]["name"], "articles": ["第五条"]}
    monkeypatch.setattr(authorities, "directory_for", lambda _: [need])
    monkeypatch.setattr(llm, "request_json", lambda *args, **kwargs: Draft(missing_authorities=[need]))
    def broken(*args):
        raise ValueError("synthetic unreadable document")
    monkeypatch.setattr(sources, "read_official", broken)
    value = runner.answer(cid, "测试来源", online_research=True)
    assert any(lead["state"] == "read_failed" for lead in value["web_sources"])
    timings = [event for event in value["trace"] if event["stage"] == "source_timing"]
    assert timings and all(event["status"] == "skipped" for event in timings)


@pytest.mark.parametrize("mime,content", [
    ("application/pdf", b"%PDF-1.7\ncorrupt"),
    ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"corrupt zip"),
])
def test_corrupt_official_attachment_preserves_supported_local_conclusions(client, corpus, monkeypatch, mime, content):
    model_settings()
    need = {"name": authorities.CATALOG[0]["name"], "articles": ["第五条"]}
    monkeypatch.setattr(authorities, "directory_for", lambda _: [])
    monkeypatch.setattr(sources, "download_official", lambda url: (url, {"content-type": mime}, content))
    def model(system, payload, schema, verify=False):
        if schema is Draft:
            row = payload["evidence"][0]
            return Draft(insufficient=False, missing_authorities=[need], claims=[{"text": "仅根据本地原文形成合成测试结论。", "kind": "rule", "evidence": [{"article_id": row["id"], "quote": row["body"]}]}])
        assert schema is Verdict
        return Verdict(checks=[{"claim_index": 0, "supported": True, "reason": "合成回归"}])
    monkeypatch.setattr(llm, "request_json", model)
    value = runner.answer(case(client), "未支付价款怎么办？", online_research=True)
    assert value["status"] == "partial" and len(value["claims"]) == 1
    assert value["citations"][0]["law_id"] == corpus["law"]["id"]
    assert any(lead["state"] == "read_failed" for lead in value["web_sources"])
    assert not value["model_errors"]


def test_sync_ask_has_job_locking_and_request_idempotency(client, monkeypatch):
    cid = case(client)
    mid = client.post(f"/api/cases/{cid}/memories", json={"text": "合成案件事实"}).json()["id"]
    entered, release = threading.Event(), threading.Event()
    def answer(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {"status": "insufficient", "case_facts": [], "research_focus": "测试"}
    monkeypatch.setattr(jobs.research_agent, "answer", answer)
    body = {"text": "同步任务测试", "request_id": "sync-repeat"}
    responses = []
    worker = threading.Thread(target=lambda: responses.append(client.post(f"/api/cases/{cid}/ask", json=body)))
    worker.start()
    try:
        assert entered.wait(2)
        assert len(client.get("/api/research").json()) == 1
        assert client.delete(f"/api/cases/{cid}").status_code == 409
        assert client.patch(f"/api/cases/{cid}", json={"title": "不能在研究中修改", "event_date": "2024-01-01", "jurisdiction": "全国"}).status_code == 409
        assert client.post(f"/api/cases/{cid}/memories", json={"text": "不能在研究中添加"}).status_code == 409
        assert client.delete(f"/api/cases/{cid}/memories/{mid}").status_code == 409
        assert client.post("/api/settings", json={"provider": "none"}).status_code == 409
        assert client.post("/api/search/settings", json={"provider": "none"}).status_code == 409
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive() and responses[0].status_code == 200
    again = client.post(f"/api/cases/{cid}/ask", json=body)
    assert again.status_code == 200 and again.json()["job_id"] == responses[0].json()["job_id"]
    assert len(client.get(f"/api/cases/{cid}").json()["messages"]) == 2


def test_stream_cancellation_checks_do_not_read_database(client, monkeypatch):
    cid = case(client)
    connections = []
    original = db.connect
    @contextmanager
    def counted():
        connections.append(True)
        with original() as connection:
            yield connection
    monkeypatch.setattr(db, "connect", counted)
    def answer(*args, cancelled, **kwargs):
        before = len(connections)
        for _ in range(10000):
            assert not cancelled()
        assert len(connections) == before
        return {"status": "insufficient", "case_facts": [], "research_focus": "测试"}
    monkeypatch.setattr(jobs.research_agent, "answer", answer)
    assert client.post(f"/api/cases/{cid}/ask", json={"text": "取消检查测试"}).status_code == 200


def test_cancel_index_before_worker_enters_body_is_terminal(client, monkeypatch):
    law(client, numbers=(1,))
    config = retrieval_services.RetrievalSettings().model_dump()
    config["embedding"].update(provider="custom", base_url="http://localhost:9998/v1", model="synthetic")
    config["auto_index"] = False
    retrieval_services.save_settings(config)
    entered, release = threading.Event(), threading.Event()
    original = indexing._worker
    def delayed():
        entered.set()
        assert release.wait(5)
        original()
    monkeypatch.setattr(indexing, "_worker", delayed)
    monkeypatch.setattr(retrieval_services, "embed", lambda *args, **kwargs: pytest.fail("cancelled index must not call service"))
    try:
        assert client.post("/api/reindex").status_code == 202
        assert entered.wait(2)
        assert client.post("/api/reindex/cancel").status_code == 200
        assert indexing.status()["job"]["status"] == "interrupted"
    finally:
        release.set()
        if indexing._thread:
            indexing._thread.join(timeout=5)
    assert indexing.status()["job"]["status"] == "interrupted"


def test_adapter_http_retries_count_towards_research_budget(client, corpus, monkeypatch):
    model_settings()
    cid = case(client)
    requests = []
    original = httpx.Client
    def handler(request):
        requests.append(request)
        if len(requests) < 3:
            return httpx.Response(503, headers={"Retry-After": "0"}, json={})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": Draft().model_dump_json()}}]})
    monkeypatch.setattr(llm.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    value = runner.answer(cid, "未支付价款怎么办？")
    assert len(requests) == value["model_calls"] == 3
    assert value["model_runs"][0]["http_attempts"] == 3
    assert sum(event["stage"] == "model_retry" for event in value["trace"]) == 2


def test_eighteen_actual_http_requests_are_a_hard_limit_including_retries(client, monkeypatch):
    model_settings(agent_rounds=3)
    law(client, numbers=(1, 2, 3))
    rows, _ = retrieval.corpus("2025-01-01", "全国")
    requests, searches = [], []
    def search(*args):
        row = rows[len(searches)]
        searches.append(True)
        return [row], []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) % 3:
            return httpx.Response(503, headers={"Retry-After": "0"}, json={})
        schema = json.loads(body["messages"][0]["content"].rsplit("JSON schema: ", 1)[1])["title"]
        payload = json.loads(body["messages"][1]["content"])
        if schema == "Draft":
            row = payload["evidence"][0]
            value = Draft(insufficient=False, claims=[{"text": "合成请求预算测试结论。", "kind": "rule", "evidence": [{"article_id": row["id"], "quote": row["body"]}]}])
        elif schema == "Verdict":
            value = Verdict(checks=[{"claim_index": 0, "supported": False, "action": "retrieve", "reason": "需要下一份合成资料"}])
        else:
            assert schema == "Plan"
            value = Plan(queries=["下一份合成资料"])
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": value.model_dump_json()}}]})
    original = httpx.Client
    monkeypatch.setattr(retrieval, "search", search)
    monkeypatch.setattr(llm.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    value = runner.answer(case(client), "请核对合成资料的责任条件。", online_research=False)
    assert len(requests) == value["model_calls"] == 18
    assert len(value["model_runs"]) == 6
    assert sum(run["http_attempts"] for run in value["model_runs"]) == 18
    assert any("18次" in warning for warning in value["warnings"])
