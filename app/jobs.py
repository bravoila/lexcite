"""Single-worker, persistent research jobs. SQLite retains progress across reloads."""
import json
import logging
import threading
from contextlib import contextmanager
from fastapi import HTTPException
from . import db
from .research_agent import context as case_state
from . import research_agent

MODEL_LOCK = threading.Lock()
_threads = set()
_threads_lock = threading.Lock()
_state_lock = threading.RLock()
_cancel_events = {}
ACTIVE = ("running", "cancel_requested")


def init():
    with db.connect() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS research_jobs(
            id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
            request_id TEXT NOT NULL, input TEXT NOT NULL, status TEXT NOT NULL,
            events TEXT NOT NULL DEFAULT '[]', message_id TEXT, error TEXT NOT NULL DEFAULT '',
            created TEXT NOT NULL, updated TEXT NOT NULL, UNIQUE(case_id,request_id))""")
        columns = {row[1] for row in c.execute("PRAGMA table_info(research_jobs)")}
        if "reasoning" not in columns:
            c.execute("ALTER TABLE research_jobs ADD COLUMN reasoning TEXT NOT NULL DEFAULT '[]'")
        c.execute("UPDATE research_jobs SET status='interrupted',error='服务重启中断了研究；未自动重复调用模型，请重新提问。',updated=? WHERE status IN ('running','cancel_requested')", (db.now(),))


def get(job_id):
    with db.connect() as c:
        row = c.execute("SELECT * FROM research_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            matches = c.execute(
                "SELECT id FROM research_jobs WHERE request_id=? ORDER BY rowid DESC LIMIT 2",
                (job_id,),
            ).fetchall()
            if len(matches) == 1:
                row = c.execute("SELECT * FROM research_jobs WHERE id=?", (matches[0]["id"],)).fetchone()
        if not row:
            raise HTTPException(404, "研究任务不存在")
        value = dict(row)
        if value["message_id"]:
            result = c.execute("SELECT payload FROM messages WHERE id=?", (value["message_id"],)).fetchone()
            value["result"] = json.loads(result[0]) if result else None
    value["events"] = json.loads(value["events"])
    value["reasoning"] = json.loads(value["reasoning"])
    value["question"] = json.loads(value.pop("input")).get("text", "")
    value.pop("request_id")
    return value


def active(case_id=None):
    with db.connect() as c:
        sql = "SELECT id FROM research_jobs WHERE status IN ('running','cancel_requested')"
        rows = c.execute(sql + (" AND case_id=?" if case_id else ""), (case_id,) if case_id else ()).fetchall()
    return [get(row[0]) for row in rows]


def latest_problem(case_id):
    with db.connect() as c:
        row = c.execute("SELECT id,status FROM research_jobs WHERE case_id=? ORDER BY rowid DESC LIMIT 1", (case_id,)).fetchone()
    return get(row["id"]) if row and row["status"] in ("failed", "cancelled", "interrupted") else None


def ensure_idle(case_id):
    if active(case_id):
        raise HTTPException(409, "本案正在研究，请先停止任务或等待完成，再修改案件和事实")


@contextmanager
def case_mutation(case_id):
    with _state_lock:
        ensure_idle(case_id)
        db.case_context(case_id)
        yield


@contextmanager
def model_slot():
    if not MODEL_LOCK.acquire(blocking=False):
        raise HTTPException(409, "当前有研究或索引任务进行中，请稍后重试")
    try:
        yield
    finally:
        MODEL_LOCK.release()


def start(case_id, value, *, background=True):
    payload = value.model_dump(exclude={"request_id"})
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    request_id = value.request_id or db.uid()
    with _state_lock:
        db.case_context(case_id)
        with db.connect() as c:
            prior = c.execute("SELECT id,input FROM research_jobs WHERE case_id=? AND request_id=?", (case_id, request_id)).fetchone()
        if prior:
            if prior["input"] != encoded:
                raise HTTPException(409, "重复请求标识对应的问题不一致")
            return get(prior["id"])
        if not MODEL_LOCK.acquire(blocking=False):
            raise HTTPException(409, "当前有研究或索引任务进行中，请稍后重试")
        jid = db.uid()
        try:
            with db.connect() as c:
                c.execute("INSERT INTO research_jobs(id,case_id,request_id,input,status,created,updated) VALUES(?,?,?,?,'running',?,?)", (jid, case_id, request_id, encoded, db.now(), db.now()))
            _cancel_events[jid] = threading.Event()
        except Exception:
            MODEL_LOCK.release()
            raise
    if background:
        thread = threading.Thread(target=_run, args=(jid, case_id, payload), name="lexcite-research", daemon=True)
        with _threads_lock:
            _threads.add(thread)
        try:
            thread.start()
        except RuntimeError:
            with _threads_lock:
                _threads.discard(thread)
            with _state_lock:
                _cancel_events.pop(jid, None)
                with db.connect() as c:
                    c.execute("UPDATE research_jobs SET status='failed',error='研究线程未能启动，请重试',updated=? WHERE id=?", (db.now(), jid))
            MODEL_LOCK.release()
            raise
    else:
        _run(jid, case_id, payload, propagate=True)
    return get(jid)


def cancel(job_id):
    with _state_lock:
        get(job_id)
        with db.connect() as c:
            changed = c.execute("UPDATE research_jobs SET status='cancel_requested',updated=? WHERE id=? AND status='running'", (db.now(), job_id)).rowcount
        event = _cancel_events.get(job_id)
        if changed and event:
            event.set()
    return get(job_id)


def _run(jid, cid, payload, *, propagate=False):
    events = []
    with _state_lock:
        cancellation = _cancel_events[jid]

    def cancelled():
        return cancellation.is_set()

    def progress(event):
        if event.get("kind") == "reasoning":
            with db.connect() as c:
                c.execute("UPDATE research_jobs SET reasoning=?,updated=? WHERE id=?", (json.dumps(event["reasoning"], ensure_ascii=False), db.now(), jid))
            return
        events.append(event)
        with db.connect() as c:
            c.execute("UPDATE research_jobs SET events=?,updated=? WHERE id=?", (json.dumps(events, ensure_ascii=False), db.now(), jid))

    def fail(status, message):
        with db.connect() as c:
            c.execute("UPDATE research_jobs SET status=?,error=?,updated=? WHERE id=?", (status, message, db.now(), jid))
    try:
        result = research_agent.answer(cid, payload["text"], payload["mode"], progress=progress, cancelled=cancelled, web_query=payload["web_query"], online_research=payload.get("online_research"))
        # Commit the pair, result, and completed state together. Repeated polling/reloads
        # cannot append another answer. Cancellation wins until this transaction commits.
        with db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT status FROM research_jobs WHERE id=?", (jid,)).fetchone()
            if not row or row[0] != "running":
                raise InterruptedError()
            user_id, mid = db.uid(), db.uid()
            for message_id, role, body in [(user_id, "user", {"text": payload["text"]}), (mid, "assistant", result)]:
                c.execute("INSERT INTO messages VALUES(?,?,?,?,?)", (message_id, cid, role, json.dumps(body, ensure_ascii=False), db.now()))
            case_state.persist(c, cid, result, user_id)
            c.execute("UPDATE research_jobs SET status='completed',message_id=?,updated=? WHERE id=?", (mid, db.now(), jid))
            c.execute("INSERT INTO audit VALUES(?,?,?,?)", (db.uid(), "answer", json.dumps({"case_id": cid, "job_id": jid, "message_id": mid, "status": result["status"]}), db.now()))
    except InterruptedError:
        fail("cancelled", "研究已停止。未保存未完成结论；正在进行的供应商请求可能仍计费。")
        if propagate:
            raise
    except ValueError as exc:
        fail("failed", str(exc))
        if propagate:
            raise
    except Exception as exc:
        logging.getLogger("lexcite").error("Research failed: %s", type(exc).__name__)
        fail("failed", "研究未完成，请检查模型配置与服务日志后重试。")
        if propagate:
            raise
    finally:
        with _state_lock:
            _cancel_events.pop(jid, None)
        MODEL_LOCK.release()
        with _threads_lock:
            _threads.discard(threading.current_thread())
