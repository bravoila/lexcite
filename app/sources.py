"""Bounded official-source reader. Every connection is pinned to a public IP.

Domain checks identify a supplied link, not the accuracy or currency of a law.
TLS still verifies the original hostname. No proxy or browser credentials are used.
"""
import http.client
import ipaddress
import re
import socket
import ssl
import time
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

MAX_SOURCE_BYTES = 8_000_000
MAX_TEXT_CHARS = 2_000_000


def official_url(value):
    value = value.strip()
    if len(value) > 1500 or re.search(r"[\x00-\x20\x7f\\]", value):
        raise ValueError("官网链接格式不正确，请复制完整的 HTTPS 原文地址")
    try:
        u = urlsplit(value)
        host, port = u.hostname or "", u.port
    except ValueError as exc:
        raise ValueError("官网链接格式不正确") from exc
    if u.scheme != "https" or not host or u.username is not None or u.password is not None or port not in (None, 443):
        raise ValueError("请使用官方原文的 HTTPS 链接（默认443端口，不含账号密码）")
    if not re.fullmatch(r"[a-z0-9.-]+", host) or not (host == "gov.cn" or host.endswith(".gov.cn") or host == "npc.gov.cn" or host.endswith(".npc.gov.cn")):
        raise ValueError("官网链接仅支持 gov.cn 或 npc.gov.cn 官方网站；本地文件可不填链接")
    return urlunsplit(("https", host, u.path or "/", u.query, u.fragment))


def public_addresses(host):
    try:
        records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("无法解析官网地址，请检查网址或改用文件导入") from exc
    addresses = list(dict.fromkeys(record[4][0] for record in records))
    if not addresses:
        raise ValueError("官网地址未解析到可用的公网地址")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise ValueError("官网地址解析到非公网地址，已停止读取")
    return addresses


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        self.pinned_address = address
        self.tls_context = ssl.create_default_context()
        super().__init__(host, timeout=timeout, context=self.tls_context)

    def connect(self):
        # Do not resolve the hostname a second time (DNS rebinding).
        raw = socket.create_connection((self.pinned_address, 443), timeout=self.timeout)
        try:
            self.sock = self.tls_context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _fetch_once(url, address, deadline):
    u = urlsplit(url)
    conn = PinnedHTTPSConnection(u.hostname, address, timeout=min(10, max(.1, deadline-time.monotonic())))
    # Non-ASCII paths are encoded; a user URL is never a shell command.
    path = quote(urlunsplit(("", "", u.path or "/", u.query, "")), safe="/%?=&:+,;@!$'()*[]~-")
    try:
        conn.request("GET", path, headers={"User-Agent": "LexCite/1.0.2", "Accept-Encoding": "identity", "Accept": "text/html,text/plain,application/pdf,*/*;q=0.5"})
        response = conn.getresponse()
        headers = {k.lower(): v for k, v in response.getheaders()}
        if response.status != 200:
            return response.status, headers, b""
        if headers.get("content-encoding", "identity").lower() not in ("", "identity"):
            raise ValueError("官网返回了暂不支持的压缩格式，请下载原文后使用文件导入")
        try:
            declared = int(headers.get("content-length", "0"))
        except ValueError:
            declared = 0
        if declared > MAX_SOURCE_BYTES:
            raise ValueError("官网内容超过8MB，请下载后使用文件导入")
        content = bytearray()
        while True:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError("source deadline")
            if conn.sock:
                conn.sock.settimeout(min(10, remaining))
            chunk = response.read1(65536)
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > MAX_SOURCE_BYTES:
                raise ValueError("官网内容超过8MB，请下载后使用文件导入")
        return response.status, headers, bytes(content)
    finally:
        conn.close()


def download_official(value):
    url = official_url(value)
    deadline = time.monotonic() + 35
    try:
        for hop in range(4):
            if time.monotonic() >= deadline:
                raise TimeoutError("source deadline")
            addresses = public_addresses(urlsplit(url).hostname)
            status, headers, content = _fetch_once(url, addresses[0], deadline)
            if status in (301, 302, 303, 307, 308):
                if hop == 3 or not headers.get("location"):
                    raise ValueError("官网跳转次数过多或跳转地址缺失，请复制最终原文链接重试")
                # Validate each destination before resolving or connecting.
                url = official_url(urljoin(url, headers["location"]))
                continue
            if status != 200:
                raise ValueError(f"官网返回 HTTP {status}，暂时无法读取。可下载文件或复制正文导入")
            return url, headers, content
    except (OSError, http.client.HTTPException) as exc:
        raise ValueError("官网连接失败或超时，请稍后重试，也可下载文件或复制正文导入") from exc
    raise ValueError("无法读取官网原文")


def decode_web_text(content, content_type):
    charset = re.search(r"charset\s*=\s*[\"']?([a-zA-Z0-9_-]+)", content_type)
    meta = re.search(rb"charset\s*=\s*[\"']?([a-zA-Z0-9_-]+)", content[:4096], re.I)
    candidates = [charset[1] if charset else "", meta[1].decode("ascii") if meta else "", "utf-8-sig", "gb18030"]
    for encoding in dict.fromkeys(candidates):
        if encoding.lower() not in ("utf-8", "utf8", "utf-8-sig", "gbk", "gb2312", "gb18030", "utf-16"):
            continue
        try:
            return content.decode(encoding)
        except UnicodeError:
            continue
    raise ValueError("官网正文编码无法识别，请转为 UTF-8 文本后导入")


def read_official(value, *, require_articles=True):
    from .ingest import TextHTML, extract_file, parse_articles
    url, headers, content = download_official(value)
    mime = headers.get("content-type", "").lower()
    path = urlsplit(url).path.lower()
    if content.startswith(b"%PDF-") or "application/pdf" in mime:
        text, name = extract_file("source.pdf", content), "官网 PDF 正文"
    elif "wordprocessingml" in mime or path.endswith(".docx"):
        text, name = extract_file("source.docx", content), "官网 DOCX 正文"
    else:
        raw = decode_web_text(content, mime)
        if "html" in mime or re.search(r"<!doctype\s+html|<html\b|<p\b|<div\b", raw[:4096], re.I):
            parser = TextHTML()
            parser.feed(raw)
            text, name = "".join(parser.parts), "官网网页正文"
        elif mime.startswith("text/") or path.endswith((".txt", ".md")):
            text, name = raw, "官网文本正文"
        else:
            raise ValueError("该链接不是可识别的网页或法规文件，请下载 TXT、PDF 或 DOCX 后导入")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError("正文超过200万字符，请按单部法规拆分")
    # A login page / JavaScript shell must not silently become a law.
    articles = []
    if require_articles:
        try:
            articles = parse_articles(text)
        except ValueError as exc:
            raise ValueError(f"未能从链接读取可用法条：{exc} 可下载文件或复制正文导入") from exc
    return {"text": text, "characters": len(text), "source_url": url,
            "source_kind": "url", "source_name": name, "count": len(articles)}
