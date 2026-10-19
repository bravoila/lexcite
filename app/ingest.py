import datetime as dt
import hashlib
import io
import re
import zlib
import zipfile
from bisect import bisect_right
from html.parser import HTMLParser
import httpx
from . import db

HSPACE = r"[^\S\r\n]"
DIGIT = r"[零〇一二三四五六七八九十百千万两0-9０-９]"
NUM = DIGIT + r"(?:" + HSPACE + r"*" + DIGIT + r")*"
ARTICLE_LABEL = r"第" + HSPACE + r"*" + NUM + HSPACE + r"*条(?:" + HSPACE + r"*之" + HSPACE + r"*" + NUM + r")?"
ARTICLE = re.compile(r"(?m)^" + HSPACE + r"*(" + ARTICLE_LABEL + r")" + HSPACE + r"*")
COMMENTARY = re.compile(r"(?m)^" + HSPACE + r"*(关于《[^》\n]{2,100}》的说明|起草说明|草案说明|修订说明|立法说明)" + HSPACE + r"*$")
STRUCTURAL_HEADING = re.compile(r"(?m)^" + HSPACE + r"*第" + HSPACE + r"*" + NUM + HSPACE + r"*(部分|编|章|节)([^\n]{0,90})$")
REFERENCE_TAIL = re.compile(r"^(?:的|之规定|所(?:规定|称|指|列|定|述)|规定(?:的|，|,|。|适用)|第"+NUM+r"[款项目]|[、，,）)]|(?:至|到|和|或|或者|及|以及|与)第)")
MAX_EXTRACTED_CHARS = 2_000_000


def normalize(text):
    return re.sub(r"\s+", "", text)


def parsing_text(text):
    text = re.sub(r"\r\n|[\r\u2028\u2029\u0085\v\f]", "\n", text)
    return text.translate({ord(c): None for c in "\ufeff\u200b\u200c\u200d\u2060"}).replace("\u00a0", " ")


def structural_headings(text):
    path, headings = [], []
    for match in STRUCTURAL_HEADING.finditer(text):
        title = normalize(match[2])
        if re.match(r"^(?:的|之|规定|所|中|内|和|与|至|到)", title):
            continue
        level = {"部分": 0, "编": 1, "章": 2, "节": 3}[match[1]]
        path = [(n, t) for n, t in path if n < level] + [(level, match[0].strip())]
        headings.append({"start": match.start(), "end": match.end(), "heading_path": " / ".join(t for _, t in path)})
    return headings


def article_key(label):
    """Unify Arabic/Chinese article numbers without changing source text."""
    def num(s):
        if s.isdigit():
            return int(s)
        digits = {c:i for i,c in enumerate("零一二三四五六七八九")}
        digits.update({"〇":0, "两":2})
        total, section, current = 0, 0, 0
        for c in s:
            if c in digits:
                current = current * 10 + digits[c]
            elif c.isdecimal():
                current = current * 10 + int(c)
            elif c in "十百千":
                section += (current or 1) * {"十":10,"百":100,"千":1000}[c]
                current = 0
            elif c == "万":
                total += (section+current or 1)*10000
                section, current = 0, 0
        return total+section+current
    m = re.fullmatch(r"第("+NUM+r")条(?:之("+NUM+r"))?", normalize(label))
    if not m:
        return (0, 0)
    main, suffix = num(m[1]), num(m[2]) if m[2] else 0
    if main <= 0 or (m[2] and suffix <= 0):
        raise ValueError("法规条号及‘之’后附加条号必须大于零，请核对原文")
    return main, suffix


def _reference_reason(text, match):
    """A line break does not turn an article reference into an article heading.

    Use explicit grammatical markers, not duplicate-number suppression. Ambiguous
    repeated headings still require manual correction, so multiple versions are
    not silently merged. The matched source text remains in its containing article.
    """
    after = normalize(text[match.end():match.end()+140])
    before = normalize(text[max(0, match.start()-180):match.start()])
    if REFERENCE_TAIL.match(after):
        return "条号后为引用连接词、款项或列举符号"
    if re.search(r"(?:依照|按照|根据|依据|参照|违反|适用|符合|依|按)(?:本法|本条例|本规定|本办法|本解释|该法|该条例|本章|《[^》]{1,100}》)?$", before):
        return "上一行以引用引导语结尾"
    if re.search(r"(?:本法|本条例|本规定|本办法|本解释)第"+NUM+r"条(?:、|，|,|至|到|和|或|及|以及|与)$", before):
        return "延续上一行的多个条号列举"
    return None


def analyze_articles(text):
    original = text
    text = parsing_text(text)
    lines = [-1] + [m.start() for m in re.finditer("\n", text)]
    line_no = lambda pos: bisect_right(lines, pos)
    warnings, references, excluded = [], [], []
    if any(c in original for c in "\ufeff\u200b\u200c\u200d\u2060"):
        warnings.append("解析副本已移除 BOM 或零宽排版字符；原文存档及 SHA256 仍使用提交原文，请核对拆分结果。")
    # Explicitly titled supplementary explanations are not statutory provisions.
    # Show the excluded heading in the preview; never hide the removal from users.
    first = ARTICLE.search(text)
    for note in COMMENTARY.finditer(text):
        if first and note.start() > first.start():
            excluded.append({"line": line_no(note.start()), "heading": note[1]})
            warnings.append(f"第{line_no(note.start())}行起为“{note[1]}”，其后说明未作为法条导入。请确认正文在此之前已完整。")
            text = text[:note.start()]
            break
    matches = []
    found = [(m, _reference_reason(text, m)) for m in ARTICLE.finditer(text)]
    for i, (match, reason) in enumerate(found):
        label_end = match.end(1)
        title_shaped = label_end == len(text) or text[label_end].isspace()
        after = normalize(text[match.end():match.end()+140])
        if reason and title_shaped and not REFERENCE_TAIL.match(after) and i + 1 < len(found):
            cur = article_key(match[1])
            nxt = article_key(found[i + 1][0][1])
            if nxt == (cur[0] + 1, 0) or nxt == (cur[0], cur[1] + 1):
                reason = None
                warnings.append(f"第{line_no(match.start())}行同时具有引用和条文标题特征，因后续条号连续暂按标题拆分，请人工核对。")
        if reason:
            references.append({"line": line_no(match.start()), "number": normalize(match[1]), "reason": reason})
        else:
            matches.append(match)
    if not matches:
        raise ValueError("未识别到法规条文标题。请确认是正式条文正文，而非目录、解读或草案说明；扫描件需先完成 OCR。")
    headings = structural_headings(text)
    result, seen, heading_paths = [], {}, {}
    for i, match in enumerate(matches):
        number = normalize(match[1])
        key = article_key(number)
        start, end = match.end(), matches[i+1].start() if i+1 < len(matches) else len(text)
        spans = [h for h in headings if start <= h["start"] < end]
        parts = []
        for heading in spans:
            parts.append(text[start:heading["start"]])
            start = heading["end"]
        body = "".join(parts + [text[start:end]]).strip()
        heading_paths[number] = next((h["heading_path"] for h in reversed(headings) if h["end"] <= match.start()), "")
        if key in seen:
            previous = seen[key]
            snippet = text[match.start():match.start()+100].strip().replace("\n", " ")
            raise ValueError(f"检测到两个疑似 {number} 标题（第{previous}行、第{line_no(match.start())}行）。当前片段：{snippet}。若此处是交叉引用，请将换行与前句连接后重新预览；若确为两个版本，请分别导入。系统未删除或覆盖任何条文。")
        if len(body) < 2 or len(body) > 24000:
            raise ValueError(f"{number} 文本长度异常，请在预览中校对")
        seen[key] = line_no(match.start())
        result.append({"number": number, "body": body, "ordinal": i})
    if len(result) > 5000:
        raise ValueError("一次请仅导入一部法规的一个版本（最多5000条）")
    if references:
        warnings.insert(0, f"识别到{len(references)}处行首或换行交叉引用，已保留在所属条文中，未作为新条文拆分。")
    return {"articles": result, "count": len(result), "warnings": warnings,
            "cross_reference_count": len(references), "cross_references": references[:30], "excluded_sections": excluded,
            "heading_paths": heading_paths}


def parse_articles(text):
    return analyze_articles(text)["articles"]


def import_law(law):
    if law.valid_to and law.valid_to <= law.valid_from:
        raise ValueError("失效日须晚于生效日；失效日当天不再纳入此版本")
    from . import indexing
    indexing.ensure_schema()
    def duplicate(connection, same):
        count = connection.execute("SELECT COUNT(*) FROM articles WHERE law_id=?", (same["id"],)).fetchone()[0]
        return {"id": same["id"], "duplicate": True, "name": same["name"], "version": same["version"],
                "count": count, "sha256": same["sha256"],
                "message": f"已存在法规《{same['name']}》{same['version']}（{same['jurisdiction']}），未重复导入，返回已有记录。"}

    identity = (law.name, law.version, law.jurisdiction)
    with db.connect() as c:
        same = c.execute("SELECT * FROM laws WHERE name=? AND version=? AND jurisdiction=?", identity).fetchone()
        if same:
            return duplicate(c, same)
    analysis = analyze_articles(law.text)
    articles = analysis["articles"]
    lid = db.uid()
    digest = hashlib.sha256(law.text.encode()).hexdigest()
    with db.connect() as c:
        inserted = c.execute("""INSERT INTO laws
            (id,name,version,jurisdiction,kind,source_url,valid_from,valid_to,reviewed_at,sha256,created,source_kind,source_name,canonical_name,document_number)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(name,version,jurisdiction) DO NOTHING""", (
            lid, law.name, law.version, law.jurisdiction, law.kind, law.source_url,
            law.valid_from.isoformat(), law.valid_to.isoformat() if law.valid_to else None,
            None, digest, db.now(), law.source_kind, law.source_name, law.canonical_name.strip(), law.document_number.strip()))
        if not inserted.rowcount:
            return duplicate(c, c.execute("SELECT * FROM laws WHERE name=? AND version=? AND jurisdiction=?", identity).fetchone())
        rows = [(db.uid(), lid, a["number"], a["body"], a["ordinal"]) for a in articles]
        c.executemany("INSERT INTO articles VALUES (?,?,?,?,?)", rows)
        indexing.mark_headings(c, [(r[0],r[2],r[3]) for r in rows], analysis)
        # Compressed snapshot of the exact submitted text: the citation card's
        # SHA-256 becomes recomputable from what is stored.
        c.execute("INSERT INTO law_snapshots VALUES (?,?)", (lid, zlib.compress(law.text.encode())))
    notes = {key: analysis[key] for key in ("warnings", "cross_reference_count", "cross_references", "excluded_sections")}
    db.audit("law_import", {"law_id": lid, "sha256": digest, "count": len(articles)} | notes)
    return {"id": lid, "count": len(articles), "sha256": digest} | notes


def extract_file(name, content):
    from pypdf.errors import PyPdfError
    from docx.opc.exceptions import PackageNotFoundError
    from docx.oxml.exceptions import InvalidXmlError
    from lxml.etree import XMLSyntaxError
    try:
        return _extract_file(name, content)
    except (PyPdfError, zipfile.BadZipFile, PackageNotFoundError, InvalidXmlError, XMLSyntaxError, KeyError, IndexError) as exc:
        raise ValueError("PDF/DOCX 文件损坏或结构不受支持，请重新下载原文或转为 UTF-8 文本后导入") from exc


def _extract_file(name, content):
    ext = name.lower().rsplit(".", 1)[-1]
    if ext in ("txt", "md"):
        for enc in ("utf-8-sig" if ext == "md" else "utf-8", "gb18030"):
            try:
                text = content.decode(enc)
                break
            except UnicodeDecodeError:
                pass
        else:
            raise ValueError("文本编码无法识别，请转为 UTF-8")
        if ext == "md":
            lines = []
            for line in text.split("\n"):
                while True:
                    cleaned = re.sub(r"^[ \t]*(?:#+|[*_+-]+|>|\d+[.)])[ \t]*", "", line)
                    if cleaned == line:
                        break
                    line = cleaned
                line = re.sub(r"^(" + ARTICLE_LABEL + r")[*_]+", r"\1", line)
                lines.append(re.sub(r"[*_]+[ \t]*$", "", line))
            text = "\n".join(lines)
        return text
    if ext == "pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise ValueError("请先解除 PDF 密码保护")
        if len(reader.pages) > 1500:
            raise ValueError("PDF超过1500页，请按法规拆分")
        pages, characters, decompressed = [], 0, 0
        for page in reader.pages:
            contents = page.get_contents()
            if contents:
                decompressed += len(contents.get_data())
                if decompressed > 50_000_000:
                    raise ValueError("PDF解压正文超过50MB，请按法规拆分或转为文本")
            text = page.extract_text() or ""
            characters += len(text)
            if characters > MAX_EXTRACTED_CHARS:
                raise ValueError("PDF提取正文超过200万字符，请按法规拆分")
            pages.append(text)
        # A mostly-blank PDF is a scan: reject explicitly instead of importing a
        # few OCR'd pages and silently dropping the rest.
        empty = sum(1 for page in pages if len(normalize(page)) < 5)
        if not pages or (empty and empty / len(pages) > 0.5):
            raise ValueError(f"此 PDF 有 {empty}/{len(pages)} 页无可读文字（扫描件特征），请先用 OCR 软件转为文字型 PDF 或 TXT")
        text = "\n".join(pages)
        if len(normalize(text)) < 30:
            raise ValueError("此 PDF 没有可读取文字，请先用 OCR 软件转为文字型 PDF 或 TXT")
        return text
    if ext == "docx":
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            if sum(i.file_size for i in z.infolist()) > 50_000_000:
                raise ValueError("DOCX解压后过大")
        from docx import Document
        from docx.oxml.ns import qn
        doc = Document(io.BytesIO(content))
        parts, seen, characters = [], set(), 0

        def append(text):
            nonlocal characters
            characters += len(text)
            if characters > MAX_EXTRACTED_CHARS:
                raise ValueError("DOCX提取正文超过200万字符，请按法规拆分")
            parts.append(text)

        def walk(element):
            if element.tag == qn("w:tc"):
                if element in seen:
                    return
                seen.add(element)
            if element.tag in (qn("w:t"), qn("a:t")):
                append(element.text or "")
            elif element.tag == qn("w:tab"):
                append("\t")
            elif element.tag in (qn("w:br"), qn("w:cr")):
                append("\n")
            else:
                for child in element:
                    walk(child)
                if element.tag == qn("w:p"):
                    append("\n")

        walk(doc.element.body)
        roots = set()
        for section in doc.sections:
            for container in (section.header, section.footer, section.first_page_header, section.first_page_footer,
                              section.even_page_header, section.even_page_footer):
                root = container._element
                if root not in roots:
                    roots.add(root)
                    walk(root)
        return "".join(parts).rstrip("\n")
    raise ValueError("支持 TXT、Markdown、文字型 PDF 和 DOCX")


class TextHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "template", "nav", "header", "footer", "aside", "form"):
            self.skip += 1
        if not self.skip and tag in ("p", "br", "div", "h1", "h2", "li", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template", "nav", "header", "footer", "aside", "form") and self.skip:
            self.skip -= 1
        if not self.skip and tag in ("p", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


CIVIL_URL = "https://www.moj.gov.cn/pub/sfbgw/zwgkztzl/2025nianzhuanti/2025mfdxcy/2025mfdxcy_mfdql/202505/t20250507_518708.html"


def fetch_civil():
    from . import sources
    page = sources.read_official(CIVIL_URL)
    text = parsing_text(page["text"])
    start = ARTICLE.search(text)
    if not start:
        raise ValueError("官网页面结构变化，请手动下载原文并导入")
    text = text[start.start():]
    # Strip page footer after the last statutory provision.
    end = re.search(r"第一千二百六十条[\s\S]*?同时废止。", text)
    if not end:
        raise ValueError("未检测到民法典结尾，拒绝自动导入不完整文本")
    text = text[:end.end()]
    articles = parse_articles(text)
    if len(articles) != 1260:
        raise ValueError(f"预期1260条，实际{len(articles)}条。请手动核对源文件后导入。")
    return {"name": "中华人民共和国民法典", "version": "2020年通过版", "valid_from": "2021-01-01",
            "source_url": page["source_url"], "text": text, "count": len(articles)}
