"""Optional search leads. A search result is never a reviewed legal authority."""
import hashlib
import json
import os
import httpx
from . import db, llm, sources, ingest
from .models import SearchSettingsInput, validate_configuration


class SearchError(Exception):
    pass


def settings(*, validate=True):
    stored = db.get("search_settings", {})
    result = SearchSettingsInput().model_dump() | stored
    env_values = {}
    for field, name in (("provider", "SEARCH_PROVIDER"), ("base_url", "SEARCH_BASE_URL"), ("timeout", "SEARCH_TIMEOUT")):
        value = os.getenv(name)
        if field not in stored and value:
            env_values[field] = value
    result.update(env_values)
    return validate_configuration(SearchSettingsInput, result, "联网搜索") if validate else result


def environment_api_key(s):
    defaults = SearchSettingsInput().model_dump()
    provider = os.getenv("SEARCH_PROVIDER") or defaults["provider"]
    endpoint = (os.getenv("SEARCH_BASE_URL") or defaults["base_url"]).rstrip("/")
    bound = s["provider"] != "none" and s["provider"] == provider and s["base_url"].rstrip("/") == endpoint
    return (os.getenv("SEARCH_API_KEY") or "") if bound and not s.get("environment_key_disabled") else ""


def public_settings():
    s = settings(validate=False)
    try:
        s = validate_configuration(SearchSettingsInput, s, "联网搜索")
    except ValueError as exc:
        s["configuration_error"] = str(exc)
    stored = db.get("search_settings", {})
    encrypted = s.pop("api_key_encrypted", "")
    uses_environment_key = not encrypted and bool(environment_api_key(s))
    s["environment_defaults"] = any(
        field not in stored and bool(os.getenv(name))
        for field, name in (("provider", "SEARCH_PROVIDER"), ("base_url", "SEARCH_BASE_URL"), ("timeout", "SEARCH_TIMEOUT"))
    )
    s["environment_api_key"] = uses_environment_key
    s["has_api_key"] = bool(uses_environment_key or encrypted)
    s.pop("api_key", None)
    s.pop("environment_key_disabled", None)
    return s


def save_settings(value):
    old = settings(validate=False)
    value = validate_configuration(SearchSettingsInput, dict(value), "联网搜索")
    key = value.pop("api_key", None)
    same = all(value.get(k) == old.get(k) for k in ("provider", "base_url"))
    if key is not None:
        value["api_key_encrypted"] = llm.cipher().encrypt(key.encode()).decode() if key else ""
    elif same and "api_key_encrypted" in old:
        value["api_key_encrypted"] = old["api_key_encrypted"]
    elif not same:
        value["api_key_encrypted"] = ""
    value["environment_key_disabled"] = key == "" or (key is None and old.get("environment_key_disabled", False))
    db.put("search_settings", value)


def search(query, check=lambda: None, timeout=None):
    # Caller supplies either user-entered public keywords or a validated legal
    # document title. Case narratives/history and model API keys are not sent.
    query = query.strip()
    if not query or len(query) > 180:
        raise SearchError("请填写180字以内、不含当事人信息的公开法律检索词")
    s = settings()
    if s["provider"] == "none":
        raise SearchError("尚未配置联网搜索，请先到模型与设置保存博查或 SearXNG 配置")
    headers, method, kwargs = {}, "GET", {}
    if s["provider"] == "bocha":
        key = ""
        if s.get("api_key_encrypted"):
            try:
                key = llm.cipher().decrypt(s["api_key_encrypted"].encode()).decode()
            except Exception as exc:
                raise SearchError("请在联网设置中保存博查 API 密钥") from exc
        else:
            key = environment_api_key(s)
        if not key:
            raise SearchError("请在联网设置中保存博查 API 密钥")
        headers["Authorization"] = "Bearer " + key
        url, method = "https://api.bochaai.com/v1/web-search", "POST"
        kwargs["json"] = {"query": query + " (site:gov.cn OR site:npc.gov.cn)", "count": 8, "summary": False}
    else:
        # This is an administrator-configured service, like a local model URL.
        # It can be on the private network; the model cannot change its endpoint.
        url = s["base_url"].rstrip("/") + "/search"
        kwargs["params"] = {"q": query + " (site:gov.cn OR site:npc.gov.cn)", "format": "json", "categories": "general"}
    check()
    try:
        with httpx.Client(timeout=min(s["timeout"], timeout or s["timeout"]), follow_redirects=False, trust_env=False) as client:
            with client.stream(method, url, headers=headers, **kwargs) as response:
                if response.status_code != 200:
                    raise SearchError(f"搜索服务返回 HTTP {response.status_code}；检查密钥、地址或 SearXNG 的 JSON 格式设置")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    check()
                    raw.extend(chunk)
                    if len(raw) > 1_000_000:
                        raise SearchError("搜索响应过大，已停止读取")
        payload = json.loads(raw)
        if s["provider"] == "bocha":
            if payload.get("code") not in (None, 200, "200"):
                raise SearchError("博查搜索未成功，请检查账户用量和配置")
            rows = payload.get("data", payload).get("webPages", {}).get("value", [])
        else:
            rows = payload.get("results", [])
        if not isinstance(rows, list):
            raise SearchError("搜索服务响应格式不正确")
        results, seen = [], set()
        for row in rows[:30]:
            if not isinstance(row, dict):
                continue
            if not isinstance(row.get("url"), str):
                continue
            try:
                url = sources.official_url(row.get("url", ""))
            except (ValueError, TypeError):
                continue
            if url in seen:
                continue
            seen.add(url)
            results.append({"url": url, "title": str(row.get("name") or row.get("title") or url)[:250],
                            "snippet": str(row.get("snippet") or row.get("content") or "")[:600],
                            "state": "search_only", "fetched_at": None,
                            "note": "搜索线索，尚未读取原文、复核版本或适用日期"})
            if len(results) >= 5:
                break
        return results
    except (SearchError, InterruptedError):
        raise
    except Exception as exc:
        raise SearchError("联网搜索失败，请检查服务是否可达、密钥及 JSON 配置；已保留本地研究结果") from exc


def read_lead(row):
    """Read at most two results in the agent; DNS/IP/redirect guards are reused."""
    try:
        page = sources.read_official(row["url"])
        articles = ingest.parse_articles(page["text"])
        previews = [a for a in articles if len(a["body"]) <= 6000][:3]
        return row | {"url": page["source_url"], "state": "read_unreviewed", "fetched_at": db.now(),
                      "article_count": len(articles), "previews": previews,
                      "sha256": hashlib.sha256(page["text"].encode()).hexdigest(),
                      "note": "已读取官网正文；版本、完整性和有效期尚待人工复核，未作为回答法源"}
    except (ValueError, httpx.HTTPError, OSError):
        return row | {"state": "read_failed", "note": "官网正文未能读取或不是法条正文；可打开来源核对，未作为回答法源"}
