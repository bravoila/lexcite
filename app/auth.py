import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import OrderedDict
from fastapi import HTTPException, Request
from . import db

LOCK = threading.Lock()
ATTEMPTS = OrderedDict()
GLOBAL_FAILS = []
LOCAL_ADDRESSES = {"127.0.0.1", "::1"}
MAX_ATTEMPT_ADDRESSES = 4096
HASH_SLOTS = threading.BoundedSemaphore(2)
_password_tag_key = secrets.token_bytes(32)
_verified_password = None
_global_next = 0.0


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return {"salt": salt, "digest": digest}


def _remember_password(password, stored):
    global _verified_password
    tag = hmac.digest(_password_tag_key, password.encode(), "sha256")
    with LOCK:
        # This verifier exists only in this process, never in the database.
        _verified_password = (stored["salt"], stored["digest"], tag)


def init():
    stored = db.get("password")
    password = os.getenv("LEXCITE_ADMIN_PASSWORD", "")
    if stored:
        if password and hmac.compare_digest(password_hash(password, stored["salt"])["digest"], stored["digest"]):
            _remember_password(password, stored)
        return
    if password:
        if len(password) < 12:
            raise RuntimeError("LEXCITE_ADMIN_PASSWORD must contain at least 12 characters")
        stored = password_hash(password)
        db.put("password", stored)
        _remember_password(password, stored)
    else:
        code_file = db.data_dir() / "setup-code.txt"
        if not code_file.exists():
            code_file.write_text(secrets.token_urlsafe(24))
            code_file.chmod(0o600)
        # Never echo the code itself: containers and CI logs are often shared.
        print("法据尚未初始化：初始化码已写入 data/setup-code.txt（仅部署管理员可读），请访问页面完成管理员密码设置。", flush=True)


def setup(password, code):
    with LOCK:
        if db.get("password"):
            raise HTTPException(409, "已初始化，请登录")
        expected = (db.data_dir() / "setup-code.txt").read_text()
        if not hmac.compare_digest(str(code), expected):
            raise HTTPException(403, "初始化码不正确，请向部署管理员获取")
        stored = password_hash(password)
        db.put("password", stored)
        (db.data_dir() / "setup-code.txt").unlink(missing_ok=True)
    _remember_password(password, stored)


def login(password, address, *, local_recovery=False):
    global _global_next
    now = time.time()
    stored = db.get("password")
    if not stored:
        raise HTTPException(401, "请先完成工作空间初始化")
    tag = hmac.digest(_password_tag_key, password.encode(), "sha256")
    local = local_recovery and os.getenv("LEXCITE_LOCAL_RECOVERY") == "1" and address in LOCAL_ADDRESSES
    with LOCK:
        for key in list(ATTEMPTS):
            attempts = [x for x in ATTEMPTS[key] if now-x < 600]
            if attempts:
                ATTEMPTS[key] = attempts
            else:
                del ATTEMPTS[key]
        GLOBAL_FAILS[:] = [x for x in GLOBAL_FAILS if now-x < 600]
        cached = _verified_password
        cache_current = cached is not None and cached[:2] == (stored["salt"], stored["digest"])
        verified = cache_current and hmac.compare_digest(tag, cached[2])
        attempts = ATTEMPTS.get(address, [])
        limited = not local and len(attempts) >= 10
        throttled = not local and len(GLOBAL_FAILS) >= 60 and now < _global_next
        retry_after = max(1, int(600 - (now-attempts[0])) + 1) if limited else 2
        if not verified and cache_current and (limited or throttled):
            raise HTTPException(429, "登录尝试过多，请稍后重试；正确密码不受匿名失败锁定", headers={"Retry-After": str(retry_after)})
        if not verified and not local and len(GLOBAL_FAILS) >= 60:
            _global_next = now + 2
    if not verified:
        if not HASH_SLOTS.acquire(blocking=False):
            raise HTTPException(429, "密码校验繁忙，请稍后重试", headers={"Retry-After": "1"})
        try:
            verified = hmac.compare_digest(password_hash(password, stored["salt"])["digest"], stored["digest"])
        finally:
            HASH_SLOTS.release()
    if not verified:
        with LOCK:
            if address not in ATTEMPTS and len(ATTEMPTS) >= MAX_ATTEMPT_ADDRESSES:
                ATTEMPTS.popitem(last=False)
            ATTEMPTS[address] = (ATTEMPTS.get(address, []) + [now])[-10:]
            ATTEMPTS.move_to_end(address)
            if not local:
                GLOBAL_FAILS.append(now)
                GLOBAL_FAILS[:] = GLOBAL_FAILS[-60:]
        if limited or throttled:
            raise HTTPException(429, "登录尝试过多，请稍后重试", headers={"Retry-After": str(retry_after)})
        raise HTTPException(401, "密码不正确")
    _remember_password(password, stored)
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
