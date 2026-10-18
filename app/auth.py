import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import defaultdict
from fastapi import HTTPException, Request
from . import db

LOCK = threading.Lock()
ATTEMPTS = defaultdict(list)
GLOBAL_FAILS = []
LOCAL_ADDRESSES = {"127.0.0.1", "::1"}


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return {"salt": salt, "digest": digest}


def init():
    if db.get("password"):
        return
    password = os.getenv("LEXCITE_ADMIN_PASSWORD", "")
    if password:
        if len(password) < 12:
            raise RuntimeError("LEXCITE_ADMIN_PASSWORD must contain at least 12 characters")
        db.put("password", password_hash(password))
    else:
        code_file = db.data_dir() / "setup-code.txt"
        if not code_file.exists():
            code_file.write_text(secrets.token_urlsafe(24))
            code_file.chmod(0o600)
        print("法据首次初始化码（仅用于创建管理员密码）: " + code_file.read_text(), flush=True)


def setup(password, code):
    with LOCK:
        if db.get("password"):
            raise HTTPException(409, "已初始化，请登录")
        expected = (db.data_dir() / "setup-code.txt").read_text()
        if not hmac.compare_digest(str(code), expected):
            raise HTTPException(403, "初始化码不正确，请向部署管理员获取")
        db.put("password", password_hash(password))
        (db.data_dir() / "setup-code.txt").unlink(missing_ok=True)


def login(password, address):
    now = time.time()
    local = address in LOCAL_ADDRESSES
    with LOCK:
        ATTEMPTS[address] = [x for x in ATTEMPTS[address] if now-x < 600]
        limited = not local and len(ATTEMPTS[address]) >= 10
        if not local:
            GLOBAL_FAILS[:] = [x for x in GLOBAL_FAILS if now-x < 600]
            throttled = len(GLOBAL_FAILS) >= 60
        else:
            throttled = False
    if limited:
        # Per-address (account) limit: only the flooded address is blocked;
        # behind a reverse proxy the addresses come from X-Forwarded-For.
        raise HTTPException(429, "登录尝试过多，请10分钟后重试")
    if throttled:
        # Global anonymous cap slows bulk guessing but never locks anyone out;
        # the correct password still works, just delayed.
        time.sleep(2)
    stored = db.get("password")
    if not stored or not hmac.compare_digest(password_hash(password, stored["salt"])["digest"], stored["digest"]):
        with LOCK:
            ATTEMPTS[address].append(now)
            if not local:
                GLOBAL_FAILS.append(now)
        raise HTTPException(401, "密码不正确")
    with LOCK:
        ATTEMPTS.pop(address, None)
    token = secrets.token_urlsafe(32)
    with db.connect() as c:
        c.execute("DELETE FROM sessions WHERE expires<?", (now,))
        c.execute("INSERT INTO sessions VALUES (?,?)", (hashlib.sha256(token.encode()).hexdigest(), now+28800))
    return token


def authenticated(request: Request):
    token = request.cookies.get("lexcite_session", "")
    if token:
        with db.connect() as c:
            row = c.execute("SELECT expires FROM sessions WHERE token=?", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if row and row[0] > time.time():
            return True
    return False


def require(request: Request):
    if not authenticated(request):
        raise HTTPException(401, "请先登录")
