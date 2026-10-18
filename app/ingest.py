import datetime as dt
import hashlib
import io
import re
from bisect import bisect_right
from html.parser import HTMLParser
import httpx
from . import db

NUM = r"[零〇一二三四五六七八九十百千万两0-9]+"
HSPACE = r"[^\S\r\n]"
ARTICLE = re.compile(r"(?m)^" + HSPACE + r"*(第" + HSPACE + r"*" + NUM + HSPACE + r"*条(?:之[一二三四五六七八九十0-9]+)?)" + HSPACE + r"*")
COMMENTARY = re.compile(r"(?m)^" + HSPACE + r"*(关于《[^》\n]{2,100}》的说明|起草说明|草案说明|修订说明|立法说明)" + HSPACE + r"*$")


def normalize(text):
    return re.sub(r"\s+", "", text)


def article_key(label):
    """Unify Arabic/Chinese article numbers without changing source text."""
    def num(s):
        if s.isdigit():
            return int(s)
        digits = {c:i for i,c in enumerate("零一二三四五六七八九")}
        digits.update({"〇":0, "两":2})
        total, current = 0, 0
        for c in s:
            if c in digits:
                current = digits[c]
            elif c in "十百千":
                total += (current or 1) * {"十":10,"百":100,"千":1000}[c]
                current = 0
            elif c == "万":
                total = (total+current)*10000
                current = 0
        return total+current
    m = re.fullmatch(r"第("+NUM+r")条(?:之("+NUM+r"))?", normalize(label))
    return (num(m[1]), num(m[2]) if m[2] else 0) if m else (0,0)


def _reference_reason(text, match):
    """A line break does not turn an article reference into an article heading.

    Use explicit grammatical markers, not duplicate-number suppression. Ambiguous
    repeated headings still require manual correction, so multiple versions are
    not silently merged. The matched source text remains in its containing article.
    """
    after = normalize(text[match.end():match.end()+140])
    before = normalize(text[max(0, match.start()-180):match.start()])
    if re.match(r"^(?:的|之规定|所(?:规定|称|指|列|定|述)|规定(?:的|，|,|。|适用)|第"+NUM+r"[款项目]|[、，,）)]|(?:至|到|和|或|或者|及|以及|与)第)", after):
        return "条号后为引用连接词、款项或列举符号"
    if re.search(r"(?:依照|按照|根据|依据|参照|违反|适用|符合|依|按)(?:本法|本条例|本规定|本办法|本解释|该法|该条例|本章|《[^》]{1,100}》)?$", before):
        return "上一行以引用引导语结尾"
    if re.search(r"(?:本法|本条例|本规定|本办法|本解释)第"+NUM+r"条(?:、|，|,|至|到|和|或|及|以及|与)$", before):
        return "延续上一行的多个条号列举"
    return None


def analyze_articles(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    lines = [-1] + [m.start() for m in re.finditer("\n", text)]
    line_no = lambda pos: bisect_right(lines, pos)
    warnings, references, excluded = [], [], []
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
    for match in ARTICLE.finditer(text):
        reason = _reference_reason(text, match)
        if reason:
            references.append({"line": line_no(match.start()), "number": normalize(match[1]), "reason": reason})
        else:
            matches.append(match)
    if not matches:
        raise ValueError("未识别到法规条文标题。请确认是正式条文正文，而非目录、解读或草案说明；扫描件需先完成 OCR。")
    result, seen = [], {}
    for i, match in enumerate(matches):
        number = normalize(match[1])
        key = article_key(number)
        body = text[match.end(): matches[i+1].start() if i+1 < len(matches) else len(text)].strip()
        # Only standalone structural headings end the previous article; a wrapped
        # reference such as '第二章规定的...' is kept verbatim.
        body = re.split(r"\n" + HSPACE + r"*第" + NUM + r"[编章节](?:" + HSPACE + r"+[^\n。；;]+|" + HSPACE + r"*)\n", body+"\n", maxsplit=1)[0].strip()
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
            "cross_reference_count": len(references), "cross_references": references[:30], "excluded_sections": excluded}


def parse_articles(text):
    return analyze_articles(text)["articles"]


def import_law(law):
    if law.valid_to and law.valid_to <= law.valid_from:
        raise ValueError("失效日须晚于生效日；失效日当天不再纳入此版本")
    from . import indexing
    indexing.ensure_schema()
    articles = parse_articles(law.text)
    lid = db.uid()
    digest = hashlib.sha256(law.text.encode()).hexdigest()
    with db.connect() as c:
        c.execute("""INSERT INTO laws
            (id,name,version,jurisdiction,kind,source_url,valid_from,valid_to,reviewed_at,sha256,created,source_kind,source_name,canonical_name,document_number)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            lid, law.name, law.version, law.jurisdiction, law.kind, law.source_url,
            law.valid_from.isoformat(), law.valid_to.isoformat() if law.valid_to else None,
            None, digest, db.now(), law.source_kind, law.source_name, law.canonical_name.strip(), law.document_number.strip()))
        rows = [(db.uid(), lid, a["number"], a["body"], a["ordinal"]) for a in articles]
        c.executemany("INSERT INTO articles VALUES (?,?,?,?,?)", rows)
        indexing.mark_headings(c, [(r[0],r[2],r[3]) for r in rows], law.text)
    db.audit("law_import", {"law_id": lid, "sha256": digest, "count": len(articles)})
    return {"id": lid, "count": len(articles), "sha256": digest}


def extract_file(name, content):
    ext = name.lower().rsplit(".", 1)[-1]
    if ext in ("txt", "md"):
        for enc in ("utf-8-sig", "gb18030"):
            try:
                return content.decode(enc)
            except UnicodeDecodeError:
                pass
        raise ValueError("文本编码无法识别，请转为 UTF-8")
    if ext == "pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise ValueError("请先解除 PDF 密码保护")
        if len(reader.pages) > 1500:
            raise ValueError("PDF超过1500页，请按法规拆分")
        text = "\n".join(p.extract_text() or "" for p in reader.pages)
        if len(normalize(text)) < 30:
            raise ValueError("此 PDF 没有可读取文字，请先用 OCR 软件转为文字型 PDF 或 TXT")
        return text
    if ext == "docx":
        import zipfile
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            if sum(i.file_size for i in z.infolist()) > 50_000_000:
                raise ValueError("DOCX解压后过大")
        from docx import Document
        doc = Document(io.BytesIO(content))
        return "\n".join(p.text for p in doc.paragraphs)
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
    # Fixed allowlisted source, no arbitrary URL fetch or redirects.
    with httpx.Client(timeout=35, follow_redirects=False) as client:
        with client.stream("GET", CIVIL_URL) as r:
            r.raise_for_status()
            chunks, size = [], 0
            for chunk in r.iter_bytes():
                size += len(chunk)
                if size > 8_000_000:
                    raise ValueError("来源页面超过8MB")
                chunks.append(chunk)
    parser = TextHTML()
    parser.feed(b"".join(chunks).decode("utf-8"))
    text = "".join(parser.parts)
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
            "source_url": CIVIL_URL, "text": text, "count": len(articles)}
