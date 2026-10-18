"""Import/parsing reliability regressions (P1-8 ~ P1-14)."""
import hashlib
import io
import json
import zlib

import pytest
from app import db, ingest
from app.ingest import analyze_articles, extract_file, parse_articles


# ---------- P1-8 章节/分编行独立识别并从 body 剔除 ----------

def test_multiple_structural_headings_stripped_from_body():
    # An article can close a section AND a chapter at once: both heading lines
    # must leave the body, not just the first one.
    rows = parse_articles("第一条 正文一。\n第二节 测试节\n第二条 正文二。\n第二节 节二\n第二章 章二\n第三条 正文三。")
    assert [r["number"] for r in rows] == ["第一条", "第二条", "第三条"]
    assert rows[0]["body"] == "正文一。"
    assert rows[1]["body"] == "正文二。"


def test_part_heading_stripped():
    rows = parse_articles("第一条 正文一。\n第一部分 总则\n第二条 正文二。")
    assert rows[0]["body"] == "正文一。"


def test_heading_path_recorded_on_import(client):
    law = {"name": "测试章节目录", "version": "v1", "jurisdiction": "全国", "kind": "法律",
           "source_kind": "paste", "valid_from": "2021-01-01",
           "text": "第一章 总则\n第一条 正文一。"}
    lid = client.post("/api/laws", json=law).json()["id"]
    from app import indexing
    rows = [r for r in indexing.articles() if r["law_id"] == lid]
    assert rows[0]["heading_path"] == "第一章 总则"


# ---------- P1-9 引用启发式不吞真实条标题 ----------

def test_lead_in_line_with_consecutive_numbers_prefers_split():
    rows = parse_articles("第一条 本条的适用，参照《中华人民共和国合同法》\n第二条 测试条文二。\n第三条 测试条文三。")
    assert [r["number"] for r in rows] == ["第一条", "第二条", "第三条"]
    assert rows[1]["body"] == "测试条文二。"


def test_acceptance_lead_in_splits():
    rows = parse_articles("第一条 办理下列事项，依照下列规定\n第二条 测试条文二。")
    assert [r["number"] for r in rows] == ["第一条", "第二条"]


def test_ambiguous_reference_kept_with_warning():
    result = analyze_articles("第一条 正文参照《中华人民共和国合同法》\n第二条 应适用该法第二条的情形。")
    assert result["count"] == 1
    assert "第二条" in result["articles"][0]["body"]
    assert any("交叉引用" in w for w in result["warnings"])


# ---------- P1-10 SHA256 可审计(原文快照 + 重算) ----------

def _import_civil(client):
    law = json.loads((db.ROOT / "examples/civil_excerpt.json").read_text())
    r = client.post("/api/laws", json=law)
    assert r.status_code == 200, r.text
    return r.json()["id"], law


def test_snapshot_recompute_matches_stored_hash(client):
    lid, law = _import_civil(client)
    r = client.get(f"/api/laws/{lid}/hash-check")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and not body["snapshot_missing"]
    assert body["recomputed"] == body["sha256"] == hashlib.sha256(law["text"].encode()).hexdigest()
    assert "UTF-8" in body["note"]


def test_snapshot_is_compressed_original(client):
    lid, law = _import_civil(client)
    with db.connect() as c:
        blob = c.execute("SELECT content FROM law_snapshots WHERE law_id=?", (lid,)).fetchone()[0]
    assert zlib.decompress(blob).decode() == law["text"]


def test_hash_check_without_snapshot_reports_missing(client):
    lid, _ = _import_civil(client)
    with db.connect() as c:
        c.execute("DELETE FROM law_snapshots WHERE law_id=?", (lid,))
    r = client.get(f"/api/laws/{lid}/hash-check").json()
    assert r["snapshot_missing"] and not r["ok"]


# ---------- P1-11 PDF 扫描件拒绝 + DOCX 不丢内容 ----------

def _minimal_pdf(page_texts):
    """Hand-built single-font PDF (one page per entry, '' = blank), so the test
    does not depend on reportlab. ASCII only; pypdf extracts the Tj literal."""
    def stream(text):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        return b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + i * 2} 0 R" for i in range(len(page_texts)))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_texts)} >>".encode())
    for i, text in enumerate(page_texts):
        page = 3 + i * 2
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {page + 1} 0 R /Resources << /Font << /F1 {3 + len(page_texts) * 2} 0 R >> >> >>".encode())
        objects.append(stream(text))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return bytes(out)


def test_scanned_pdf_rejected_with_clear_error():
    pdf = _minimal_pdf(["some readable provision text", "", ""])
    with pytest.raises(ValueError, match="扫描件"):
        extract_file("扫描.pdf", pdf)


def test_text_pdf_extracts_all_pages():
    pdf = _minimal_pdf(["some readable provision text", "another page of text"])
    text = extract_file("正文.pdf", pdf)
    assert "readable provision text" in text and "another page of text" in text


def test_docx_includes_tables_and_headers():
    from docx import Document
    doc = Document()
    doc.add_paragraph("第一条 正文一。")
    table = doc.add_table(rows=1, cols=1)
    table.cell(0, 0).text = "第二条 表格内条文。"
    doc.sections[0].header.paragraphs[0].text = "第三条 页眉条文。"
    buf = io.BytesIO()
    doc.save(buf)
    text = extract_file("正文.docx", buf.getvalue())
    assert "第一条 正文一。" in text and "第二条 表格内条文。" in text and "第三条 页眉条文。" in text
    assert text.index("第一条") < text.index("第二条")


def test_extract_endpoint_accepts_docx_off_event_loop(client):
    from docx import Document
    doc = Document()
    doc.add_paragraph("第一条 正文一。")
    buf = io.BytesIO()
    doc.save(buf)
    r = client.post("/api/extract", files={"file": ("正文.docx", buf.getvalue(),
                                                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    assert r.status_code == 200 and "第一条 正文一。" in r.json()["text"]


# ---------- P1-13 重复导入查重返回已有记录 ----------

def test_duplicate_import_returns_existing_record(client, corpus):
    law = json.loads((db.ROOT / "examples/civil_excerpt.json").read_text())
    r = client.post("/api/laws", json=law)
    assert r.status_code == 200
    d = r.json()
    assert d["duplicate"] and d["id"] == corpus["law"]["id"] and "已存在法规" in d["message"]
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM laws").fetchone()[0] == 1


def test_same_text_different_metadata_imports_separately(client, corpus):
    # 相同正文但不同名称/版本是合法的独立版本(版本重叠复核场景),不得被查重误拦。
    law = json.loads((db.ROOT / "examples/civil_excerpt.json").read_text())
    law["name"] = "民法典别称测试"
    r = client.post("/api/laws", json=law)
    assert r.status_code == 200
    d = r.json()
    assert not d.get("duplicate") and d["id"] != corpus["law"]["id"]


# ---------- P1-14 Markdown 标记剥离 ----------

def test_markdown_markers_stripped_before_split():
    md = "# 第一条 正文一。\n* 第二条 正文二。\n- 第三条 正文三。"
    rows = parse_articles(extract_file("法规.md", md.encode()))
    assert [r["number"] for r in rows] == ["第一条", "第二条", "第三条"]


def test_txt_content_untouched():
    txt = "# 第一条 正文一。"
    assert extract_file("法规.txt", txt.encode()) == txt


def test_markdown_extraction_roundtrip_matches_plaintext_split():
    plain = "第一条 正文一。\n第二条 正文二。"
    md = "## 第一条 正文一。\n- 第二条 正文二。"
    assert parse_articles(extract_file("法规.md", md.encode())) == parse_articles(plain)
