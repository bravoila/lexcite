import datetime as dt
from app import db
from app.research_agent import runner as engine, retrieval
from app.ingest import parse_articles
from app.research_agent.schemas import Claim
import pytest


def test_exact_citation_and_fabrication(client, corpus):
    a = corpus["articles"][0]
    claim = Claim(text="违反合同义务可能承担违约责任。",kind="rule",evidence=[{"article_id":a["id"],"quote":a["body"]}])
    assert engine.validate_claim(claim, [a])[0]
    claim.evidence[0].quote = "赔偿金额一律为十万元，不得调整。"
    assert not engine.validate_claim(claim, [a])[0]
    claim.evidence[0].quote = a["body"]
    claim.evidence[0].article_id = "invented"
    assert not engine.validate_claim(claim,[a])[0]


def test_unretrieved_article_number(client,corpus):
    a=corpus["articles"][0]
    claim=Claim(text="根据第九千条应当赔偿。",kind="rule",evidence=[{"article_id":a["id"],"quote":a["body"]}])
    assert not engine.validate_claim(claim,[a])[0]


def test_blank_quote_cannot_pass_integrity(client, corpus):
    a = corpus["articles"][0]
    claim = Claim(text="测试结论", kind="rule", evidence=[{"article_id": a["id"], "quote": " \t\n\u3000    "}])
    assert not engine.validate_claim(claim, [a])[0]


def test_preserve_exceptions_and_reject_duplicate():
    articles=parse_articles("第一条 应当履行，但是不能履行的除外。\n（一）法律上不能履行。\n第二章 特别规定\n第二条 第二条的正文。")
    assert "但是" in articles[0]["body"] and "（一）" in articles[0]["body"]
    assert "第二章" not in articles[0]["body"]
    with pytest.raises(ValueError):
        parse_articles("第一条 原文甲。\n第一条 原文乙。")


def test_temporal_and_jurisdiction_filters(client,corpus):
    assert retrieval.corpus("2020-12-31","全国")[0] == []
    assert len(retrieval.corpus("2021-01-01","全国")[0])==3
    lid=corpus["law"]["id"]
    with db.connect() as c:
        c.execute("UPDATE laws SET jurisdiction='上海市',valid_to='2025-01-01' WHERE id=?",(lid,))
    assert not retrieval.corpus("2024-12-31","北京市")[0]
    assert len(retrieval.corpus("2024-12-31","上海市")[0])==3
    assert not retrieval.corpus("2025-01-01","上海市")[0]


def test_unreviewed_and_stale_excluded(client,corpus):
    with db.connect() as c:
        c.execute("UPDATE laws SET reviewed_at=NULL")
    assert not retrieval.corpus("2025-01-01","全国")[0]
    with db.connect() as c:
        c.execute("UPDATE laws SET reviewed_at='2000-01-01T00:00:00Z'")
    assert not retrieval.corpus("2025-01-01","全国")[0]


def test_overlapping_versions_excluded(client,corpus):
    import json
    value=json.loads((db.ROOT/"examples/civil_excerpt.json").read_text());value["version"]="other-version"
    lid=client.post("/api/laws",json=value).json()["id"]
    client.post(f"/api/laws/{lid}/review",json={"confirmed":True,"valid_from":"2021-01-01"})
    rows,warnings=retrieval.corpus("2025-01-01","全国")
    assert not rows and any("多个版本" in w for w in warnings)


def test_retrieves_payment_article(client,corpus):
    hits,_=retrieval.search(["对方未支付合同价款，可以请求其支付吗？"],"2025-01-01","全国")
    assert hits and hits[0]["number"]=="第五百七十九条"


def test_dimension_mismatch():
    assert retrieval.cosine([1,2],[1,2,3]) == -1


def test_arabic_article_reference(client,corpus):
    from app.ingest import article_key
    assert article_key("第一千零四条") == (1004,0)
    assert article_key("第134条之一") == (134,1)
    hits,_=retrieval.search(["民法典第577条"],"2025-01-01","全国")
    assert hits[0]["number"]=="第五百七十七条"


def test_fabricated_law_title(client,corpus):
    row=corpus["articles"][0] | {"name":corpus["law"]["name"]}
    claim=Claim(text="《虚构法律》规定可以获得赔偿。",kind="rule",evidence=[{"article_id":row["id"],"quote":row["body"]}])
    assert not engine.validate_claim(claim,[row])[0]


def _single_law(client,text):
    value={"name":"测试单法","version":"v1","jurisdiction":"全国","kind":"法律",
           "source_kind":"paste","valid_from":"2021-01-01","text":text}
    lid=client.post("/api/laws",json=value).json()["id"]
    assert client.post(f"/api/laws/{lid}/review",json={"confirmed":True,"valid_from":"2021-01-01"}).status_code==200
    return lid


def test_foreign_law_reference_not_exact_in_single_library(client):
    _single_law(client,"第二百六十四条 在测试情形下应当承担相应责任，具体内容仅用于检索回归测试。")
    hits,warnings=retrieval.search(["刑法第264条"],"2025-01-01","全国")
    assert not any(r["number"]=="第二百六十四条" for r in hits)
    assert any("未入库法规" in w and "刑法" in w for w in warnings)
    hits,_=retrieval.search(["第264条"],"2025-01-01","全国")
    assert hits and hits[0]["number"]=="第二百六十四条" and "exact" in hits[0]["retrieval_channels"]


def test_zhisuffix_arabic_chinese_interop(client):
    _single_law(client,"第一百三十四条 基础条文正文……\n第一百三十四条之一 之条文正文……")
    hits,_=retrieval.search(["第134条之1"],"2025-01-01","全国")
    assert any(r["number"]=="第一百三十四条之一" and "exact" in r["retrieval_channels"] for r in hits)
    hits,_=retrieval.search(["第134条"],"2025-01-01","全国")
    assert any(r["number"]=="第一百三十四条" for r in hits)
    assert not any(r["number"]=="第一百三十四条之一" for r in hits)


def test_cross_reference_expansion_normalized(client):
    # Query hits 第500条 whose body references 本法第一百二十三条; the referenced
    # article is titled 第123条 (Arabic). Raw-string comparison would miss it.
    rows=[{"id":"r500","law_id":"L","name":"测试法","version":"v1","kind":"法律","heading_path":"","ordinal":0,
           "number":"第500条","body":"本条设置测试内容，具体适用见本法第一百二十三条的补充规则。"},
          {"id":"r123","law_id":"L","name":"测试法","version":"v1","kind":"法律","heading_path":"","ordinal":1,
           "number":"第123条","body":"补充规则正文内容，仅用于同法交叉引用展开回归测试。"}]
    detail={}
    with retrieval.observe(detail.update):
        selected,warnings=retrieval.rank(["第500条"],rows)
    assert any(r["id"]=="r123" and r.get("expanded") for r in selected)


def test_claim_article_numeral_forms_normalized(client,corpus):
    a=next(x for x in corpus["articles"] if x["number"]=="第五百七十七条")
    claim=Claim(text="根据第577条应当支付价款。",kind="rule",evidence=[{"article_id":a["id"],"quote":a["body"]}])
    assert engine.validate_claim(claim,[a])[0]
    claim=Claim(text="根据第五百七十七条应当支付价款。",kind="rule",evidence=[{"article_id":a["id"],"quote":a["body"]}])
    assert engine.validate_claim(claim,[a])[0]


def test_zhisuffix_verification():
    row={"id":"r1","number":"第134条之一","body":"第一百三十四条之一正文，用于之条款校验回归测试。","name":"测试法"}
    claim=Claim(text="根据第134条之1应当承担相应责任。",kind="rule",evidence=[{"article_id":"r1","quote":row["body"]}])
    assert engine.validate_claim(claim,[row])[0]
    claim=Claim(text="根据第134条应当承担相应责任。",kind="rule",evidence=[{"article_id":"r1","quote":row["body"]}])
    assert not engine.validate_claim(claim,[row])[0]


def test_bare_law_name_binding(client,corpus):
    a=next(x for x in corpus["articles"] if x["number"]=="第五百七十七条") | {"name":corpus["law"]["name"]}
    claim=Claim(text="根据民法典第五百七十七条应当支付价款。",kind="rule",evidence=[{"article_id":a["id"],"quote":a["body"]}])
    assert engine.validate_claim(claim,[a],["民法典"])[0]
    other={"id":"other-577","number":"第五百七十七条","body":"测试他法第五百七十七条正文，用于裸名称绑定回归测试。","name":"测试他法"}
    claim=Claim(text="根据民法典第五百七十七条应当支付价款。",kind="rule",evidence=[{"article_id":"other-577","quote":other["body"]}])
    assert not engine.validate_claim(claim,[other],["民法典"])[0]
