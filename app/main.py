from .law_names import display_title
from contextlib import asynccontextmanager
import datetime as dt
import hashlib
import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, Depends, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from . import auth, db, ingest, llm, sources, jobs, websearch, indexing, retrieval_services
from .research_agent import retrieval, context as case_state
from . import research_agent as engine
from .models import AskInput, CaseInput, LawInput, LawURLInput, LawPreviewInput, MemoryInput, SettingsInput, SearchSettingsInput
from .research_agent.schemas import Plan

logger = logging.getLogger("lexcite")
MODEL_LOCK = jobs.MODEL_LOCK
APP_VERSION = "1.3.2"


@asynccontextmanager
async def lifespan(app):
    db.init()
    jobs.init()
    auth.init()
    llm.cipher()
    retrieval_services.init()
    indexing.init()
    yield
    indexing.stop()


app = FastAPI(title="法据 API", version=APP_VERSION, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def protection(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get("X-Lexcite") != "1":
            return JSONResponse({"detail": "缺少请求校验标记"}, status_code=403)
        origin = request.headers.get("origin")
        allowed = {str(request.base_url).rstrip("/")}
        if os.getenv("LEXCITE_PUBLIC_URL"):
            allowed.add(os.environ["LEXCITE_PUBLIC_URL"].rstrip("/"))
        if origin and origin.rstrip("/") not in allowed:
            return JSONResponse({"detail": "请求来源不匹配"}, status_code=403)
        try:
            content_length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"detail": "无效请求长度"}, status_code=400)
        if content_length > 22_000_000:
            return JSONResponse({"detail": "文件过大，最大20MB"}, status_code=413)
        # Enforce the same bound for chunked uploads before JSON/multipart parsing.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 22_000_000:
                return JSONResponse({"detail": "请求内容超过限制"}, status_code=413)
        request._body = bytes(body)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    if request.url.path == "/" or request.url.path.startswith(("/api/", "/static/", "/assets/")):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(ValueError)
async def value_error(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(llm.ModelError)
async def model_error(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=502)


@app.exception_handler(sqlite3.IntegrityError)
async def integrity_error(request, exc):
    return JSONResponse({"detail": "同名法规、版本和地区已存在，或目标记录不存在"}, status_code=409)


@app.exception_handler(Exception)
async def unexpected_error(request, exc):
    logger.error("Request failed: %s", type(exc).__name__)
    return JSONResponse({"detail": "服务处理失败，操作未完成。请联系部署管理员查看日志。"}, status_code=500)


@app.get("/healthz")
def health():
    return {"status": "ok", "version": APP_VERSION}


@app.get("/api/session")
def session(request: Request):
    return {"initialized": bool(db.get("password")), "authenticated": auth.authenticated(request), "version": APP_VERSION}


class LoginInput(BaseModel):
    password: str = Field(min_length=1, max_length=200)
    setup_code: str = Field(default="", max_length=200)


@app.post("/api/setup")
def setup(value: LoginInput):
    if len(value.password) < 12:
        raise HTTPException(400, "密码至少12个字符")
    auth.setup(value.password, value.setup_code)
    return {"ok": True}


@app.post("/api/login")
def login(value: LoginInput, request: Request):
    token = auth.login(value.password, request.client.host if request.client else "unknown")
    response = JSONResponse({"ok": True})
    response.set_cookie("lexcite_session", token, httponly=True, samesite="strict", secure=os.getenv("LEXCITE_SECURE_COOKIE") == "1", max_age=28800)
    return response


@app.post("/api/logout", dependencies=[Depends(auth.require)])
def logout(request: Request):
    token = request.cookies.get("lexcite_session", "")
    with db.connect() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (hashlib.sha256(token.encode()).hexdigest(),))
    response = JSONResponse({"ok": True})
    response.delete_cookie("lexcite_session")
    return response


@app.get("/api/status", dependencies=[Depends(auth.require)])
def status():
    with db.connect() as c:
        count = c.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        laws = c.execute("SELECT COUNT(*) FROM laws").fetchone()[0]
        reviewed = c.execute("SELECT COUNT(*) FROM laws WHERE reviewed_at IS NOT NULL").fetchone()[0]
    return {"articles": count, "laws": laws, "reviewed": reviewed, "model": llm.public_settings(), "retrieval": retrieval_services.public_settings()}


@app.get("/api/cases", dependencies=[Depends(auth.require)])
def cases():
    with db.connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM cases ORDER BY created DESC")]


@app.post("/api/cases", dependencies=[Depends(auth.require)])
def create_case(value: CaseInput):
    cid = db.uid()
    with db.connect() as c:
        c.execute("INSERT INTO cases VALUES (?,?,?,?,?)", (cid, value.title, value.event_date.isoformat(), value.jurisdiction, db.now()))
    return {"id": cid}


@app.patch("/api/cases/{cid}", dependencies=[Depends(auth.require)])
def update_case(cid: str, value: CaseInput):
    jobs.ensure_idle(cid)
    db.case_context(cid)
    with db.connect() as c:
        c.execute("UPDATE cases SET title=?,event_date=?,jurisdiction=? WHERE id=?", (value.title, value.event_date.isoformat(), value.jurisdiction, cid))
    return {"ok": True}


@app.delete("/api/cases/{cid}", dependencies=[Depends(auth.require)])
def delete_case(cid: str):
    jobs.ensure_idle(cid)
    with db.connect() as c:
        c.execute("DELETE FROM cases WHERE id=?", (cid,))
    db.audit("case_delete", {"id": cid})
    return {"ok": True}


@app.get("/api/cases/{cid}", dependencies=[Depends(auth.require)])
def case_detail(cid: str):
    case, memories, _ = db.case_context(cid)
    with db.connect() as c:
        messages = [dict(r) for r in c.execute("SELECT * FROM messages WHERE case_id=? ORDER BY rowid", (cid,))]
    for message in messages:
        message["payload"] = json.loads(message["payload"])
    return {"case": case, "memories": memories, "messages": messages, "observed_facts": case_state.load(cid, "", memories)["facts"], "active_jobs": jobs.active(cid), "last_problem": jobs.latest_problem(cid)}


@app.post("/api/cases/{cid}/memories", dependencies=[Depends(auth.require)])
def memory(cid: str, value: MemoryInput):
    jobs.ensure_idle(cid)
    db.case_context(cid)
    with db.connect() as c:
        count = c.execute("SELECT COUNT(*) FROM memories WHERE case_id=?", (cid,)).fetchone()[0]
        if count >= 30:
            raise HTTPException(400, "每案最多30条已确认事实，请先整理已有记录")
        mid = db.uid()
        c.execute("INSERT INTO memories VALUES (?,?,?,?)", (mid, cid, value.text, db.now()))
    return {"id": mid}


@app.delete("/api/cases/{cid}/memories/{mid}", dependencies=[Depends(auth.require)])
def delete_memory(cid: str, mid: str):
    jobs.ensure_idle(cid)
    with db.connect() as c:
        c.execute("DELETE FROM memories WHERE id=? AND case_id=?", (mid,cid))
    return {"ok": True}


@app.post("/api/cases/{cid}/ask", dependencies=[Depends(auth.require)])
def ask(cid: str, value: AskInput):
    db.case_context(cid)
    if not MODEL_LOCK.acquire(blocking=False):
        raise HTTPException(409, "当前有分析或索引任务进行中，请稍后重试")
    try:
        # The current question is provided separately and appended only after completion.
        result = engine.answer(cid, value.text, value.mode, web_query=value.web_query, online_research=value.online_research)
        user_id, mid = db.uid(), db.uid()
        with db.connect() as c:
            for message_id, role, body in [(user_id, "user", {"text": value.text}), (mid, "assistant", result)]:
                c.execute("INSERT INTO messages VALUES(?,?,?,?,?)", (message_id, cid, role, json.dumps(body, ensure_ascii=False), db.now()))
            case_state.persist(c, cid, result, user_id)
        db.audit("answer", {"case_id": cid, "message_id": mid, "status": result["status"], "citations": [r["id"] for r in result["citations"]]})
        return result | {"message_id": mid}
    finally:
        MODEL_LOCK.release()


@app.post("/api/cases/{cid}/research", status_code=202, dependencies=[Depends(auth.require)])
def start_research(cid: str, value: AskInput):
    return jobs.start(cid, value)


@app.get("/api/research", dependencies=[Depends(auth.require)])
def active_research():
    return jobs.active()


@app.get("/api/research/{jid}", dependencies=[Depends(auth.require)])
def research_status(jid: str):
    return jobs.get(jid)


@app.post("/api/research/{jid}/cancel", dependencies=[Depends(auth.require)])
def cancel_research(jid: str):
    return jobs.cancel(jid)


@app.get("/api/search/settings", dependencies=[Depends(auth.require)])
def search_settings():
    return websearch.public_settings()


@app.post("/api/search/settings", dependencies=[Depends(auth.require)])
def save_search_settings(value: SearchSettingsInput):
    if MODEL_LOCK.locked():
        raise HTTPException(409, "请等待当前研究完成后修改搜索设置")
    websearch.save_settings(value.model_dump())
    return websearch.public_settings()


@app.post("/api/search/test", dependencies=[Depends(auth.require)])
def test_search():
    try:
        rows = websearch.search("中华人民共和国民法典")
        return {"ok": True, "message": f"搜索接口正常，本次找到 {len(rows)} 个官网线索。"}
    except websearch.SearchError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/cases/{cid}/export", dependencies=[Depends(auth.require)])
def export(cid: str):
    value = case_detail(cid)
    lines = [f"# {value['case']['title']}", f"研究日期：{value['case']['event_date']} / 地区：{value['case']['jurisdiction']}", "\n## 已确认事实"]
    lines += ["- " + m["text"] for m in value["memories"]]
    for m in value["messages"]:
        p = m["payload"]
        if m["role"] == "user":
            lines += ["\n## 提问", p["text"]]
        else:
            lines += ["\n## 回答", p.get("summary", ""), f"状态：{p['status']} / 生成时间：{p['generated_at']}"]
            if p["claims"]:
                lines += ["\n### 基于现有资料的结论"]
            lines += ["- " + c["text"] + " " + " ".join(f"[{n}]" for n in c["citations"]) for c in p["claims"]]
            if p.get("retrieval_runs"):
                lines += ["\n### 本次实际检索方式", *["- "+x["summary"] for x in p["retrieval_runs"]]]
            if p.get("missing_facts"):
                lines += ["\n### 待补充", *["- "+x for x in p["missing_facts"]]]
            if p.get("source_gaps") or p.get("legal_issues"):
                lines += ["\n### 推荐补充资料", "以下为研究目录或模型提出的补充建议，不作为本次结论依据；名称、条号及适用性仍需核对。"]
                for gap in p.get("source_gaps",[]):
                    lines += ["- "+display_title(gap["name"])+"、".join(gap.get("articles",[]))+"："+gap.get("detail",gap.get("reason",""))]
                    if gap.get("reason") and gap["reason"]!=gap.get("detail"):
                        lines += ["建议补充原因："+gap["reason"]]
                    if gap.get("online_detail"):
                        lines += ["联网补查："+gap["online_detail"]]
                lines += ["- "+issue for issue in p.get("legal_issues",[])]
            if p.get("case_facts"):
                lines += ["\n### 本轮采用的用户陈述（未自动核实）", *["- "+x["state"]+"："+x["text"] for x in p["case_facts"]]]
            # Raw reasoning is deliberately kept in the separate UI record;
            # the legal research export contains only the checked result/trace.
            lines += ["\n### 引用原文"]
            for r in p["citations"]:
                lines += [f"\n[{r['label']}] {display_title(r['name'])}{r['number']}（{r['version']}）", r["body"],
                          f"正文来源：{r.get('source_name', '导入正文')}",
                          f"官网链接：{r.get('source_url') or '未提供；由导入人核对来源'}", f"有效期：{r['valid_from']} 至 {r['valid_to'] or '未录入失效日'}；复核：{r['reviewed_at']}", f"源文本 SHA256：{r['sha256']}"]
                if r.get("authority_status") == "official_auto_checked":
                    lines += ["来源级别：官网临时原文，未人工复核；读取于 "+r["fetched_at"], r["version_note"], *r.get("temporal_provisions",[])]
                if r.get("version_basis"):
                    basis=r["version_basis"]
                    lines += ["修正案日期依据："+basis["name"],basis["url"],"版本施行日期："+basis["effective_from"],"日期依据 SHA256："+basis["sha256"]]
            if p.get("web_sources"):
                lines += ["\n### 官网来源与线索（是否纳入临时证据见各项状态）"]
                for lead in p["web_sources"]:
                    lines += [lead["title"], lead["url"], lead["note"]]
            if p.get("trace"):
                lines += ["\n### 研究步骤"]
                for event in p["trace"]:
                    lines += [str(event["step"]) + "：" + str(event["detail"])]
            lines += ["\n" + p.get("notice", ""), *["提示："+x for x in p["warnings"]]]
    return Response("\n\n".join(lines), media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="legal-research.md"'})


@app.get("/api/laws", dependencies=[Depends(auth.require)])
def laws():
    with db.connect() as c:
        return [dict(r) for r in c.execute("SELECT l.*,COUNT(a.id) AS article_count FROM laws l LEFT JOIN articles a ON a.law_id=l.id GROUP BY l.id ORDER BY l.created DESC")]


@app.get("/api/laws/{lid}", dependencies=[Depends(auth.require)])
def law_detail(lid: str):
    with db.connect() as c:
        row = c.execute("SELECT * FROM laws WHERE id=?", (lid,)).fetchone()
        if not row:
            raise HTTPException(404, "法规不存在")
        return {"law": dict(row), "articles": [dict(r) for r in c.execute("SELECT * FROM articles WHERE law_id=? ORDER BY ordinal", (lid,))]}


@app.post("/api/laws/preview", dependencies=[Depends(auth.require)])
def preview(value: LawPreviewInput):
    return ingest.analyze_articles(value.text)


@app.post("/api/laws", dependencies=[Depends(auth.require)])
def import_law(value: LawInput):
    result = ingest.import_law(value)
    indexing.start()
    return result


@app.post("/api/extract", dependencies=[Depends(auth.require)])
async def extract(file: UploadFile = File(...)):
    content = await file.read(20_000_001)
    await file.close()
    if len(content) > 20_000_000:
        raise HTTPException(413, "文件最大20MB")
    try:
        text = ingest.extract_file(file.filename or "", content)
        if not text.strip():
            raise ValueError("未从文件中提取到正文，请使用文字型文件或先完成 OCR")
        if len(text) > sources.MAX_TEXT_CHARS:
            raise ValueError("正文超过200万字符，请按单部法规拆分")
    except ValueError:
        raise
    except Exception as exc:
        raise HTTPException(400, "文件解析失败，请转成UTF-8文本再导入") from exc
    name = (file.filename or "上传文件").replace("\\", "/").rsplit("/", 1)[-1]
    name = " ".join(name.split())[:255] or "上传文件"
    return {"text": text, "characters": len(text), "source_kind": "file", "source_name": name}


@app.post("/api/extract-url", dependencies=[Depends(auth.require)])
def extract_url(value: LawURLInput):
    try:
        return sources.read_official(value.url)
    except ValueError:
        raise
    except Exception as exc:
        raise HTTPException(400, "官网正文解析失败，请下载原文或复制正文后导入") from exc


@app.post("/api/official/civil-code", dependencies=[Depends(auth.require)])
def official():
    try:
        return ingest.fetch_civil()
    except Exception as exc:
        raise HTTPException(502, "官方页面当前无法完整读取（可能有访问限制）。请打开司法部原文，下载或复制正文后导入。") from exc


class ReviewInput(BaseModel):
    confirmed: bool
    valid_from: dt.date
    valid_to: dt.date | None = None
    canonical_name: str | None = Field(default=None, max_length=200)
    document_number: str | None = Field(default=None, max_length=100)


@app.post("/api/laws/{lid}/review", dependencies=[Depends(auth.require)])
def review(lid: str, value: ReviewInput):
    law_detail(lid)
    if not value.confirmed:
        raise HTTPException(400, "请先核对官方来源、完整条文和有效期")
    if value.valid_to and value.valid_to <= value.valid_from:
        raise HTTPException(400, "失效日应晚于生效日")
    with db.connect() as c:
        c.execute("UPDATE laws SET reviewed_at=?,valid_from=?,valid_to=? WHERE id=?", (db.now(), value.valid_from.isoformat(), value.valid_to.isoformat() if value.valid_to else None, lid))
        if value.canonical_name is not None:
            c.execute("UPDATE laws SET canonical_name=? WHERE id=?", (value.canonical_name.strip(), lid))
        if value.document_number is not None:
            c.execute("UPDATE laws SET document_number=? WHERE id=?", (value.document_number.strip(), lid))
    indexing.start()
    db.audit("law_review", {"law_id": lid, "valid_from": str(value.valid_from), "valid_to": str(value.valid_to)})
    return {"ok": True}


@app.delete("/api/laws/{lid}", dependencies=[Depends(auth.require)])
def delete_law(lid: str):
    with db.connect() as c:
        c.execute("DELETE FROM laws WHERE id=?", (lid,))
    db.audit("law_delete", {"law_id": lid})
    return {"ok": True}


@app.get("/api/settings", dependencies=[Depends(auth.require)])
def get_settings():
    return llm.public_settings()


@app.post("/api/settings", dependencies=[Depends(auth.require)])
def save_settings(value: SettingsInput):
    if MODEL_LOCK.locked():
        raise HTTPException(409, "请等待当前任务结束后再修改模型配置")
    llm.save_settings(value.model_dump())
    db.audit("model_settings", {"provider": value.provider, "model": value.model})
    return llm.public_settings()


@app.post("/api/settings/test", dependencies=[Depends(auth.require)])
def model_test():
    seen = []
    with llm.capture_reasoning(lambda fragment: seen.append(True) if not seen else None):
        result = llm.request_json("你是连接测试助手。返回JSON：queries数组仅包含连接成功四个字。", {"test": True}, Plan)
    message = "模型连接和结构化输出正常" if result.queries else "接口可用，但输出内容为空"
    message += "；本次接口返回了思考字段" if seen else "；本次未返回思考字段（不影响使用）"
    return {"ok": bool(result.queries), "reasoning_returned": bool(seen), "message": message}


@app.get("/api/retrieval/settings", dependencies=[Depends(auth.require)])
def get_retrieval_settings():
    return retrieval_services.public_settings()


@app.post("/api/retrieval/settings", dependencies=[Depends(auth.require)])
def save_retrieval_settings(value: retrieval_services.RetrievalSettings):
    if not MODEL_LOCK.acquire(blocking=False):
        raise HTTPException(409, "请等待研究或索引结束，再修改检索配置")
    try:
        result = retrieval_services.save_settings(value.model_dump())
        db.audit("retrieval_settings", {"mode": value.mode, "embedding": value.embedding.model, "reranker": value.reranker.model})
    finally:
        MODEL_LOCK.release()
    indexing.start()
    return result


@app.get("/api/retrieval/status", dependencies=[Depends(auth.require)])
def retrieval_status():
    return indexing.status()


@app.post("/api/retrieval/test/{kind}", dependencies=[Depends(auth.require)])
def retrieval_test(kind: str):
    if kind not in ("embedding", "reranker"):
        raise HTTPException(404, "未知检索服务")
    if not MODEL_LOCK.acquire(blocking=False):
        raise HTTPException(409, "请等待研究或索引任务完成")
    try:
        if kind == "embedding":
            vector = retrieval_services.embed(["连接测试：查找合同履行相关条文"], query=True)[0]
            return {"ok": True, "message": f"向量接口正常，实际维度 {len(vector)}；还需建立法条索引。"}
        scores = retrieval_services.rerank("合同履行", ["合同履行相关条文", "其他事项"])
        return {"ok": True, "message": f"重排接口正常，返回 {len(scores)} 项有效分数；此测试不代表法律检索质量。"}
    finally:
        MODEL_LOCK.release()


@app.post("/api/reindex", status_code=202, dependencies=[Depends(auth.require)])
def reindex():
    indexing.start(manual=True)
    return {"queued": True}


@app.post("/api/reindex/cancel", dependencies=[Depends(auth.require)])
def cancel_index():
    indexing.cancel()
    return {"ok": True}


@app.get("/api/example", dependencies=[Depends(auth.require)])
def example():
    return json.loads((db.ROOT / "examples" / "civil_excerpt.json").read_text())


@app.get("/assets/{version}/{name}")
def versioned_asset(version: str, name: str):
    if version != APP_VERSION or name not in ("app.js", "style.css"):
        raise HTTPException(404, "资源版本与服务不匹配，请刷新网页或完整更新程序")
    return FileResponse(db.ROOT / "static" / name)


app.mount("/static", StaticFiles(directory=db.ROOT / "static"), name="static")


@app.get("/")
def home():
    return FileResponse(db.ROOT / "static" / "index.html")
