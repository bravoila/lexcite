import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import uuid

ROOT = Path(__file__).resolve().parents[1]


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def uid():
    return uuid.uuid4().hex


def data_dir():
    p = Path(os.getenv("LEXCITE_DATA", str(ROOT / "data")))
    p.mkdir(parents=True, exist_ok=True)
    # Cases, memories and the encrypted API key live here; keep the directory
    # readable only by the service account.
    try:
        os.chmod(p, 0o700)
    except OSError:
        pass
    return p


@contextlib.contextmanager
def connect():
    path = data_dir() / "lexcite.sqlite3"
    c = sqlite3.connect(path, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init():
    with connect() as c:
        c.execute("PRAGMA journal_mode=WAL")
        # WAL/SHM sidecars are created lazily on first write; tighten them once
        # journal_mode materializes the files.
        for suffix in ("-wal", "-shm"):
            try:
                os.chmod(data_dir() / f"lexcite.sqlite3{suffix}", 0o600)
            except OSError:
                pass
        c.executescript('''
        CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS cases(
            id TEXT PRIMARY KEY,title TEXT NOT NULL,event_date TEXT NOT NULL,
            jurisdiction TEXT NOT NULL DEFAULT '全国',created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS messages(
            id TEXT PRIMARY KEY,case_id TEXT REFERENCES cases(id) ON DELETE CASCADE,
            role TEXT NOT NULL,payload TEXT NOT NULL,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS memories(
            id TEXT PRIMARY KEY,case_id TEXT REFERENCES cases(id) ON DELETE CASCADE,
            text TEXT NOT NULL,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS laws(
            id TEXT PRIMARY KEY,name TEXT NOT NULL,version TEXT NOT NULL,
            jurisdiction TEXT NOT NULL,kind TEXT NOT NULL,source_url TEXT NOT NULL,
            valid_from TEXT NOT NULL,valid_to TEXT,reviewed_at TEXT,
            sha256 TEXT NOT NULL,created TEXT NOT NULL,
            UNIQUE(name,version,jurisdiction));
        CREATE TABLE IF NOT EXISTS articles(
            id TEXT PRIMARY KEY,law_id TEXT NOT NULL REFERENCES laws(id) ON DELETE CASCADE,
            number TEXT NOT NULL,body TEXT NOT NULL,ordinal INTEGER NOT NULL,
            UNIQUE(law_id,number));
        CREATE TABLE IF NOT EXISTS vectors(
            article_id TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
            fingerprint TEXT NOT NULL,vector TEXT NOT NULL,
            PRIMARY KEY(article_id,fingerprint));
        CREATE TABLE IF NOT EXISTS audit(
            id TEXT PRIMARY KEY,action TEXT NOT NULL,details TEXT NOT NULL,created TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_messages_case ON messages(case_id,created);
        CREATE INDEX IF NOT EXISTS idx_article_law ON articles(law_id);
        CREATE INDEX IF NOT EXISTS idx_law_eligibility ON laws(jurisdiction,valid_from,valid_to,reviewed_at);
        ''')
        # Additive, repeatable migration: existing data and citation snapshots stay intact.
        columns = {row[1] for row in c.execute("PRAGMA table_info(laws)")}
        if "source_kind" not in columns:
            c.execute("ALTER TABLE laws ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'paste'")
        for name, definition in (("canonical_name", "TEXT NOT NULL DEFAULT ''"), ("document_number", "TEXT NOT NULL DEFAULT ''")):
            if name not in columns:
                c.execute(f"ALTER TABLE laws ADD COLUMN {name} {definition}")
        c.execute("CREATE TABLE IF NOT EXISTS case_state_snapshots(case_id TEXT PRIMARY KEY REFERENCES cases(id) ON DELETE CASCADE,payload TEXT NOT NULL)")
        if "source_name" not in columns:
            c.execute("ALTER TABLE laws ADD COLUMN source_name TEXT NOT NULL DEFAULT '导入正文'")
        for table in ("laws", "articles"):
            corpus_triggers(c, table)


def corpus_triggers(connection, table):
    if table not in ("laws", "articles", "article_headings"):
        raise ValueError("未知法库表")
    for action in ("INSERT", "UPDATE", "DELETE"):
        connection.execute(f"""CREATE TRIGGER IF NOT EXISTS corpus_{table}_{action.lower()}
            AFTER {action} ON {table} BEGIN
            INSERT INTO kv(key,value) VALUES('corpus_revision','1')
            ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1;
            END""")


def corpus_version():
    return str((data_dir() / "lexcite.sqlite3").resolve()), int(get("corpus_revision", 0))


def get(key, default=None):
    with connect() as c:
        row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def put(key, value):
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))


def audit(action, details):
    with connect() as c:
        c.execute("INSERT INTO audit VALUES (?,?,?,?)", (uid(), action, json.dumps(details, ensure_ascii=False), now()))


def add_message(case_id, role, payload):
    mid = uid()
    with connect() as c:
        c.execute("INSERT INTO messages VALUES (?,?,?,?,?)", (mid, case_id, role, json.dumps(payload, ensure_ascii=False), now()))
    return mid


def case_context(case_id):
    with connect() as c:
        row = c.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if not row:
            raise ValueError("案件不存在")
        memories = [dict(x) for x in c.execute("SELECT * FROM memories WHERE case_id=? ORDER BY created", (case_id,))]
        # Only prior USER utterances are recalled. Previous generated answers are never evidence.
        history = [json.loads(x[0])["text"] for x in c.execute(
            "SELECT payload FROM messages WHERE case_id=? AND role='user' ORDER BY rowid DESC LIMIT 4", (case_id,))]
    return dict(row), memories, list(reversed(history))
