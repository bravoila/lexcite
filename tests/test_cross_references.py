import pytest
from app.ingest import analyze_articles, parse_articles


@pytest.mark.parametrize("reference", [
    "第一百四十一条的规定办理。",
    "第一百四十一条规定的情形。",
    "第一百四十一条第二款执行。",
    "第一百四十一条、第142条的规定执行。",
    "第一百四十一条至第一百四十三条的规定办理。",
    "第一百四十一条\n的规定办理。",
    "第一百四十一条 所规定的情形。",
    "第一百四十一条，参照办理。",
])
def test_wrapped_reference_is_preserved_not_split(reference):
    source = f"第一百四十一条 这是用于测试的正文，并非真实法规。\n第一百四十二条 本条依照本法\n{reference}\n第一百四十三条 这是后续测试条文。"
    result = analyze_articles(source)
    assert result["count"] == 3
    assert result["cross_reference_count"] == 1
    assert reference in result["articles"][1]["body"]
    assert result["articles"][2]["number"] == "第一百四十三条"


def test_forward_reference_and_inline_reference_do_not_create_articles():
    source = "第一条 本条引用第一百四十一条，并参照\n第999条所规定的情形办理。\n第二条 这是后续的测试条文。"
    rows = parse_articles(source)
    assert [r["number"] for r in rows] == ["第一条", "第二条"]
    assert "第一百四十一条" in rows[0]["body"] and "第999条" in rows[0]["body"]


def test_standalone_article_labels_and_compact_headings_still_work():
    rows = parse_articles("第141条\n这是第一条的测试正文。\n第一百四十二条这是第二条的测试正文。")
    assert len(rows) == 2 and rows[0]["body"] == "这是第一条的测试正文。"


def test_chapter_reference_not_truncated():
    rows = parse_articles("第一条 应当依照\n第二章规定的规则办理，不得省略例外。\n第二章 特别规定\n第二条 本条仅用于测试。")
    assert "第二章规定的规则办理，不得省略例外。" in rows[0]["body"]
    assert "第二章 特别规定" not in rows[0]["body"]


@pytest.mark.parametrize("second", ["第一百四十一条", "第141条"])
def test_real_repeated_headings_are_not_silently_removed(second):
    with pytest.raises(ValueError, match="第1行、第3行") as error:
        parse_articles(f"第一百四十一条 旧版本的测试条文。\n第一百四十二条 后续测试条文。\n{second} 新版本的测试条文。")
    assert "新版本" in str(error.value)


def test_explicit_supplementary_explanation_is_separate_and_reported():
    source = "第一条 本条仅用于测试。\n第二条 后续测试条文。\n关于《测试法（草案）》的说明\n一、立法经过。\n第一百四十一条的规定已经作了调整。"
    result = analyze_articles(source)
    assert result["count"] == 2
    assert "立法经过" not in result["articles"][-1]["body"]
    assert result["excluded_sections"][0]["line"] == 3
    assert any("说明未作为法条" in warning for warning in result["warnings"])


def test_preview_only_needs_text_saving_requires_metadata(client):
    payload = {"text": "第一条 本条只用于验证导入流程。"}
    assert client.post("/api/laws/preview", json=payload).json()["count"] == 1
    assert client.post("/api/laws", json=payload).status_code == 422


def test_versioned_assets_and_no_cache(client):
    for path in ("/", "/assets/1.4.0/app.js", "/assets/1.4.0/style.css", "/static/app.js"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert client.get("/api/session").json()["version"] == "1.4.0"
    assert client.get("/assets/1.0.1/app.js").status_code == 404
    html = client.get("/").text
    assert '/assets/1.4.0/app.js' in html and '/static/app.js' not in html
