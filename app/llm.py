from contextlib import contextmanager
from contextvars import ContextVar
from email.utils import parsedate_to_datetime
import time
import hashlib
import json
import math
import logging
import os
import re
import socket
import threading
import httpx
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from . import db
from .models import SettingsInput, validate_configuration


load_dotenv(db.ROOT / ".env", override=False)
_control = ContextVar("model_control", default=None)
_reasoning = ContextVar("provider_reasoning", default=None)
_usage = ContextVar("provider_usage", default=None)
_request_options = ContextVar("model_request_options", default=None)
_attempts = ContextVar("model_http_attempts", default=None)
MAX_RESPONSE_BYTES = 4_000_000


@contextmanager
def controlled(check, deadline):
    token = _control.set((check, deadline))
    try:
        yield
    finally:
        _control.reset(token)


@contextmanager
def capture_reasoning(callback):
    token = _reasoning.set(callback)
    try:
        yield
    finally:
        _reasoning.reset(token)


def report_reasoning(text):
    callback = _reasoning.get()
    if callback and isinstance(text, str) and text:
        callback(text)


@contextmanager
def capture_usage(callback):
    token = _usage.set(callback)
    try:
        yield
    finally:
        _usage.reset(token)


@contextmanager
def capture_attempts(callback):
    token = _attempts.set(callback)
    try:
        yield
    finally:
        _attempts.reset(token)


@contextmanager
def request_options(**options):
    token = _request_options.set(options)
    try:
        yield
    finally:
        _request_options.reset(token)


def report_usage(value):
    callback = _usage.get()
    if callback and isinstance(value, dict):
        callback({key: value[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                  if type(value.get(key)) is int and value[key] >= 0})


class ModelError(Exception):
    def __init__(self, message, *, fatal=False, status_code=None, code="", retry_after=None):
        super().__init__(message)
        self.fatal = fatal
        self.status_code = status_code
        self.code = code
        self.retry_after = retry_after


def profile_defaults(profile):
    prefix = "MODEL_LOCAL" if profile == "local" else "MODEL_CLOUD"
    provider_default = "ollama" if profile == "local" else "dashscope"
    base_default = "http://host.docker.internal:11434/v1" if profile == "local" else "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model_default = "qwen3:8b" if profile == "local" else "qwen-plus"
    return {
        "provider": os.getenv(f"{prefix}_PROVIDER") or os.getenv("MODEL_PROVIDER") or provider_default,
        "base_url": (os.getenv(f"{prefix}_BASE_URL") or os.getenv("MODEL_BASE_URL") or base_default).rstrip("/"),
        "model": os.getenv(f"{prefix}_NAME") or os.getenv("MODEL_NAME") or model_default,
    }


def profile_api_key(profile):
    prefix = "MODEL_LOCAL" if profile == "local" else "MODEL_CLOUD"
    return os.getenv(f"{prefix}_API_KEY") or os.getenv("MODEL_API_KEY") or ""


def settings(*, validate=True):
    stored = db.get("settings", {})
    s = SettingsInput().model_dump()
    s.update(stored)
    profile = stored.get("profile")
    if not profile and stored.get("provider") == "ollama":
        profile = "local"
    elif not profile and stored.get("provider") in ("dashscope", "deepseek", "siliconflow"):
        profile = "api"
    profile = profile or os.getenv("MODEL_PROFILE", "local")
    s["profile"] = profile
    for field, value in profile_defaults(profile if profile in ("local", "api") else "local").items():
        if field not in stored:
            s[field] = value
    s["api_key"] = s.get("api_key") or ""
    return validate_configuration(SettingsInput, s, "模型") if validate else s


def environment_api_key(s):
    if s["profile"] not in ("local", "api"):
        return ""
    defaults = profile_defaults(s["profile"])
    bound = all(s.get(key, "").rstrip("/") == defaults[key] for key in ("provider", "base_url"))
    return profile_api_key(s["profile"]) if bound and not s.get("environment_key_disabled") else ""


def public_settings():
    s = settings(validate=False)
    try:
        s = validate_configuration(SettingsInput, s, "模型")
    except ValueError as exc:
        s["configuration_error"] = str(exc)
    stored = db.get("settings", {})
    profile = s["profile"]
    encrypted = s.pop("api_key_encrypted", "")
    s.pop("api_key", None)
    uses_environment_key = not encrypted and bool(environment_api_key(s))
    s["has_api_key"] = bool(encrypted or uses_environment_key)
    s["environment_api_key"] = uses_environment_key
    s.pop("api_key_encrypted", None)
    s.pop("environment_key_disabled", None)
    s["environment_defaults"] = any(field not in stored for field in ("provider", "base_url", "model")) or uses_environment_key
    s["profiles"] = {
        name: profile_defaults(name) | {"environment_api_key": bool(profile_api_key(name))}
        for name in ("local", "api")
    }
    return s


def save_settings(value):
    old = settings(validate=False)
    value = dict(value)
    if "profile" not in value:
        value["profile"] = "local" if value.get("provider") == "ollama" else "api" if value.get("provider") in ("dashscope", "deepseek", "siliconflow") else old["profile"]
    value = validate_configuration(SettingsInput, value, "模型")
    key = value.pop("api_key", None)
    # The stored key is bound to its endpoint; changing profile/provider/base_url
    # without supplying a new key clears it instead of leaking it to a new host.
    same = all(value.get(k) == old.get(k) for k in ("profile", "provider", "base_url"))
    if key is not None:
        value["api_key_encrypted"] = cipher().encrypt(key.encode()).decode() if key else ""
    elif same and "api_key_encrypted" in old:
        value["api_key_encrypted"] = old["api_key_encrypted"]
    else:
        value["api_key_encrypted"] = ""
    value["environment_key_disabled"] = key == "" or (key is None and old.get("environment_key_disabled", False))
    db.put("settings", value)


def cipher():
    p = db.data_dir() / "secret.key"
    if not p.exists():
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        except FileExistsError:
            pass
    p.chmod(0o600)
    return Fernet(p.read_bytes())


def resolved():
    s = settings()
    if s.get("api_key_encrypted"):
        try:
            s["api_key"] = cipher().decrypt(s["api_key_encrypted"].encode()).decode()
        except (ValueError, UnicodeError, InvalidToken) as e:
            raise ModelError("模型密钥无法解密，请在设置中重新保存") from e
    else:
        s["api_key"] = environment_api_key(s)
    return s


def parse_json(text):
    text = re.sub(r"^\s*<think>[\s\S]*?</think>\s*", "", text).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def response_chunks(response, check, limit=MAX_RESPONSE_BYTES):
    size = 0
    for chunk in response.iter_bytes():
        check()
        size += len(chunk)
        if size > limit:
            raise ModelError("模型响应超过大小限制")
        yield chunk
    check()


@contextmanager
def interruptible_response(response, check):
    stream = response.extensions.get("network_stream")
    connection = stream.get_extra_info("socket") if stream else None
    if connection is None:
        yield
        return
    stopped, interruption = threading.Event(), []

    def watch():
        while not stopped.wait(.05):
            try:
                check()
            except (ModelError, InterruptedError) as exc:
                interruption.append(exc)
                # shutdown, unlike close alone, interrupts a concurrent blocked recv.
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError as error:
                    logging.getLogger("lexcite").debug("Model connection already closed during interruption: %s", type(error).__name__)
                return

    watcher = threading.Thread(target=watch, name="lexcite-model-deadline", daemon=True)
    watcher.start()
    try:
        yield
    finally:
        stopped.set()
        watcher.join()
        if interruption:
            raise interruption[0]


def response_message(response, streaming, check, secret=""):
    chunks = response_chunks(response, check)
    if not streaming or "application/json" in response.headers.get("content-type", "").lower():
        result = json.loads(b"".join(chunks))
        report_usage(result.get("usage"))
        choice = result["choices"][0]
        message = choice["message"]
        report_reasoning(message.get("reasoning_content") or message.get("reasoning"))
        return message.get("content") or "", choice.get("finish_reason")

    def lines():
        pending = bytearray()
        for chunk in chunks:
            pending.extend(chunk)
            while b"\n" in pending:
                line, _, remaining = pending.partition(b"\n")
                pending = bytearray(remaining)
                yield line.rstrip(b"\r").decode("utf-8")
        if pending:
            yield pending.decode("utf-8")

    content, finish_reason = [], None
    for line in lines():
        check()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        chunk = json.loads(data)
        report_usage(chunk.get("usage"))
        if chunk.get("error"):
            error = chunk["error"]
            raise ModelError("模型流式接口报错：" + safe_error(error.get("message", "服务未完成响应") if isinstance(error, dict) else error, secret))
        choices = chunk.get("choices", [])
        if choices:
            choice = choices[0]
            delta = choice.get("delta", {})
            report_reasoning(delta.get("reasoning_content") or delta.get("reasoning"))
            content.append(delta.get("content") or "")
            finish_reason = choice.get("finish_reason") or finish_reason
    return "".join(content), finish_reason


def wait_before_retry(delay, check):
    end = time.monotonic() + delay
    while time.monotonic() < end:
        check()
        time.sleep(max(0, min(.1, end-time.monotonic())))
    check()


def request_json(system, payload, schema, verify=False):
    s = resolved()
    s.update(_request_options.get() or {})
    if s["provider"] == "none":
        raise ModelError("未配置模型")
    model = s["verify_model"] if verify and s["verify_model"] else s["model"]
    control = _control.get()
    if control:
        control[0]()
    body = {"model": model, "temperature": 0, "max_tokens": s["max_tokens"], "stream": False,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system + "\n最终答案只输出一个合法JSON对象，不包含内部思考过程。\nJSON schema: " + json.dumps(schema.model_json_schema(), ensure_ascii=False)},
                         {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]}
    thinking = s["thinking_mode"]
    if thinking != "auto":
        enabled = thinking == "enabled"
        if s["provider"] in ("dashscope", "siliconflow"):
            body["enable_thinking"] = enabled
        elif s["provider"] == "deepseek":
            body["thinking"] = {"type": thinking}
        elif s["provider"] in ("ollama", "custom"):
            body["reasoning_effort"] = "high" if enabled else "none"
    # Some Qwen endpoints/versions reject forced JSON in thinking mode. Prompt
    # JSON still goes through the same local schema and citation validation.
    if s["json_mode"] == "prompt" or (s["json_mode"] == "auto" and s["provider"] == "dashscope" and thinking != "disabled"):
        body.pop("response_format", None)
    body["stream"] = (s["provider"] == "dashscope" and thinking != "disabled") or bool(_reasoning.get() and s["show_reasoning"])
    if body["stream"] and s["provider"] in ("dashscope", "deepseek", "siliconflow"):
        body["stream_options"] = {"include_usage": True}
    headers = {"Content-Type": "application/json"}
    if s["api_key"]:
        headers["Authorization"] = "Bearer " + s["api_key"]
    deadline = min(time.monotonic()+s["timeout"], control[1] if control else float("inf"))

    def check():
        if control:
            control[0]()
        if time.monotonic() >= deadline:
            raise ModelError("模型调用已达到单次墙钟时限")

    try:
        with httpx.Client(timeout=s["timeout"], follow_redirects=False, trust_env=False) as client:
            for attempt in range(1, 4):
                check()
                observer = _attempts.get()
                if observer:
                    observer(attempt)
                try:
                    with client.stream("POST", s["base_url"] + "/chat/completions", json=body, headers=headers,
                                       timeout=max(.001, deadline-time.monotonic())) as response:
                        with interruptible_response(response, check):
                            check_status(response, s["api_key"], check=check)
                            content, finish_reason = response_message(response, body["stream"], check, s["api_key"])
                    break
                except ModelError as exc:
                    if exc.status_code not in (429, 500, 502, 503, 504) or attempt == 3:
                        raise
                    delay = exc.retry_after if exc.retry_after is not None else .5 * 2 ** (attempt-1)
                    if delay >= deadline-time.monotonic():
                        raise ModelError(str(exc)+"；重试等待超出本次调用剩余时限，未继续请求",
                                         status_code=exc.status_code, code=exc.code) from exc
                    wait_before_retry(delay, check)
            check()
            if body["stream"] and finish_reason != "stop":
                raise ModelError("模型流式输出未完整结束，请检查输出上限、网络或模型兼容性")
            if finish_reason == "length":
                raise ModelError("模型输出被截断，请提高输出长度或缩小问题范围")
            if finish_reason not in ("stop", None):
                raise ModelError("模型未完整返回答案")
            # Older local models may expose reasoning in a leading <think> block.
            # Only consume a leading block, never text inside the JSON answer.
            if isinstance(content, str):
                match = re.match(r"\s*<think>([\s\S]*?)</think>\s*", content)
                if match:
                    report_reasoning(match.group(1))
                    content = content[match.end():]
            return schema.model_validate(parse_json(content))
    except (ModelError, InterruptedError):
        raise
    except (httpx.HTTPError, AttributeError, KeyError, IndexError, TypeError, ValueError) as e:
        raise ModelError("本次模型调用未返回完整结构化结果，请检查模式、输出长度和接口兼容性") from e


def safe_error(message, secret=""):
    text = str(message)
    if secret:
        text = text.replace(secret, "[密钥已隐藏]")
    text = re.sub(r"(?i)Bearer\s+[^\s,;]+|sk-[A-Za-z0-9_-]+", "[密钥已隐藏]", text)
    return text[:650]


def check_status(response, secret="", *, check=None):
    status = response.status_code
    if status < 300:
        return
    code, message = "", "请核对服务地址、模型权限和请求参数"
    control = _control.get()
    check = check or (control[0] if control else lambda: None)
    retry_after = None
    header = response.headers.get("retry-after", "")
    if header:
        try:
            retry_after = max(0, float(header))
            if not math.isfinite(retry_after):
                retry_after = None
        except ValueError:
            try:
                retry_after = max(0, parsedate_to_datetime(header).timestamp()-time.time())
            except (ValueError, TypeError, OverflowError):
                retry_after = None
    try:
        if response.is_stream_consumed:
            payload = response.json()
        else:
            raw = bytearray()
            for chunk in response.iter_bytes():
                check()
                raw.extend(chunk[:16000-len(raw)])
                if len(raw) >= 16000:
                    break
            payload = json.loads(raw)
        error = payload.get("error", payload)
        if isinstance(error, dict):
            code = safe_error(error.get("code", ""), secret)
            message = safe_error(error.get("message", message), secret)
    except (AttributeError, ValueError, TypeError, UnicodeError, httpx.HTTPError):
        message = "服务未返回可读取的错误说明；请核对服务地址、模型权限和请求参数"
    hint = ""
    if status in (400, 422):
        hint = "；可检查‘输出格式’与思考模式兼容性、模型输出长度及服务地址"
    raise ModelError(f"模型接口 HTTP {status}" + (f"（{code}）" if code else "") + f"：{message}{hint}",
                     fatal=status in (400, 401, 402, 403, 404, 422), status_code=status, code=code, retry_after=retry_after)


def embedding_fingerprint(s=None):
    """Compatibility wrapper; new indexes use independent retrieval configuration."""
    from . import retrieval_services
    return retrieval_services.fingerprint()


def embed(texts):
    from . import retrieval_services
    return retrieval_services.embed(texts)
