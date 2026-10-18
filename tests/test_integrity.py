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
