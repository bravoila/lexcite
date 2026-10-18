from contextlib import contextmanager
from contextvars import ContextVar
import time
import hashlib
import json
import os
import re
import httpx
from dotenv import load_dotenv
from . import db
from .models import SettingsInput


load_dotenv(db.ROOT / ".env", override=False)
_control = ContextVar("model_control", default=None)
_reasoning = ContextVar("provider_reasoning", default=None)
_usage = ContextVar("provider_usage", default=None)
_request_options = ContextVar("model_request_options", default=None)


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
    def __init__(self, message, *, fatal=False, status_code=None, code=""):
        super().__init__(message)
        self.fatal = fatal
        self.status_code = status_code
        self.code = code


def profile_defaults(profile):
    prefix = "MODEL_LOCAL" if profile == "local" else "MODEL_CLOUD"
    provider_default = "ollama" if profile == "local" else "dashscope"
    base_default = "http://host.docker.internal:11434/v1" if profile == "local" else "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model_default = "qwen3:8b" if profile == "local" else "qwen-plus"
    configured = SettingsInput(
        provider=os.getenv(f"{prefix}_PROVIDER") or os.getenv("MODEL_PROVIDER") or provider_default,
        base_url=os.getenv(f"{prefix}_BASE_URL") or os.getenv("MODEL_BASE_URL") or base_default,
        model=os.getenv(f"{prefix}_NAME") or os.getenv("MODEL_NAME") or model_default,
    )
    return {key: configured.model_dump()[key] for key in ("provider", "base_url", "model")}


def profile_api_key(profile):
    prefix = "MODEL_LOCAL" if profile == "local" else "MODEL_CLOUD"
    return os.getenv(f"{prefix}_API_KEY") or os.getenv("MODEL_API_KEY") or ""


def settings():
    stored = db.get("settings", {})
    s = SettingsInput().model_dump()
    s.update(stored)
    profile = stored.get("profile")
    if not profile and stored.get("provider") == "ollama":
        profile = "local"
    elif not profile and stored.get("provider") in ("dashscope", "deepseek", "siliconflow"):
        profile = "api"
    profile = profile or os.getenv("MODEL_PROFILE", "local")
    if profile not in ("local", "api"):
        raise RuntimeError("MODEL_PROFILE must be either 'local' or 'api'")
    s["profile"] = profile
    for field, value in profile_defaults(profile).items():
        if field not in stored:
            s[field] = value
    s["api_key"] = s.get("api_key") or ""
    return s


def public_settings():
    s = settings()
    stored = db.get("settings", {})
    profile = s["profile"]
    encrypted = s.pop("api_key_encrypted", "")
    s.pop("api_key", None)
    uses_environment_key = not encrypted and bool(profile_api_key(profile))
    s["has_api_key"] = bool(encrypted or uses_environment_key)
    s["environment_api_key"] = uses_environment_key
    s.pop("api_key_encrypted", None)
    s["environment_defaults"] = any(field not in stored for field in ("provider", "base_url", "model")) or uses_environment_key
    s["profiles"] = {
        name: profile_defaults(name) | {"environment_api_key": bool(profile_api_key(name))}
        for name in ("local", "api")
    }
    return s


def save_settings(value):
    from cryptography.fernet import Fernet
    old = db.get("settings", {})
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
    db.put("settings", value)


def cipher():
    from cryptography.fernet import Fernet
    p = db.data_dir() / "secret.key"
    if not p.exists():
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        except FileExistsError:
            pass
    return Fernet(p.read_bytes())


def resolved():
    s = settings()
    if s.get("api_key_encrypted"):
        try:
            s["api_key"] = cipher().decrypt(s["api_key_encrypted"].encode()).decode()
        except Exception as e:
            raise ModelError("模型密钥无法解密，请在设置中重新保存") from e
    else:
        s["api_key"] = profile_api_key(s["profile"])
    return s


def parse_json(text):
    text = re.sub(r"^\s*<think>[\s\S]*?</think>\s*", "", text).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


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
    try:
        timeout = s["timeout"]
        if control:
            timeout = max(.1, min(timeout, control[1] - time.monotonic()))
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            if body["stream"]:
                content, finish_reason, byte_count = "", None, 0
                with client.stream("POST", s["base_url"] + "/chat/completions", json=body, headers=headers) as r:
                    check_status(r, s["api_key"])
                    received_json = "application/json" in r.headers.get("content-type", "").lower()
                    if received_json:
                        raw = bytearray()
                        for chunk in r.iter_bytes():
                            if control:
                                control[0]()
                            raw.extend(chunk)
                            if len(raw) > 4_000_000:
                                raise ModelError("模型响应超过大小限制")
                        result = json.loads(raw)
                        report_usage(result.get("usage"))
                        choice = result["choices"][0]
                        finish_reason = choice.get("finish_reason")
                        message = choice["message"]
                        report_reasoning(message.get("reasoning_content") or message.get("reasoning"))
                        content = message.get("content") or ""
                    for line in (() if received_json else r.iter_lines()):
                        if control:
                            control[0]()
                        byte_count += len(line)
                        if byte_count > 4_000_000:
                            raise ModelError("模型响应超过大小限制")
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        chunk = json.loads(data)
                        report_usage(chunk.get("usage"))
                        if chunk.get("error"):
                            error = chunk["error"]
                            raise ModelError("模型流式接口报错：" + safe_error(error.get("message", "服务未完成响应") if isinstance(error, dict) else str(error), s["api_key"]))
                        choices = chunk.get("choices", [])
                        if not choices:
                            continue
                        choice = choices[0]
                        delta = choice.get("delta", {})
                        report_reasoning(delta.get("reasoning_content") or delta.get("reasoning"))
                        content += delta.get("content") or ""
                        finish_reason = choice.get("finish_reason") or finish_reason
                if finish_reason != "stop":
                    raise ModelError("模型流式输出未完整结束，请检查输出上限、网络或模型兼容性")
            else:
                r = client.post(s["base_url"] + "/chat/completions", json=body, headers=headers)
                check_status(r, s["api_key"])
                result = r.json()
                report_usage(result.get("usage"))
                choice = result["choices"][0]
                finish_reason = choice.get("finish_reason")
                content = choice["message"]["content"]
                report_reasoning(choice["message"].get("reasoning_content") or choice["message"].get("reasoning"))
            if control:
                control[0]()
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
    except Exception as e:
        raise ModelError("本次模型调用未返回完整结构化结果，请检查模式、输出长度和接口兼容性") from e


def safe_error(message, secret=""):
    text = str(message)
    if secret:
        text = text.replace(secret, "[密钥已隐藏]")
    text = re.sub(r"(?i)Bearer\s+[^\s,;]+|sk-[A-Za-z0-9_-]+", "[密钥已隐藏]", text)
    return text[:650]


def check_status(response, secret=""):
    status = response.status_code
    if status < 300:
        return
    code, message = "", "请核对服务地址、模型权限和请求参数"
    try:
        if response.is_stream_consumed:
            payload = response.json()
        else:
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > 16000:
                    break
            payload = json.loads(raw)
        error = payload.get("error", payload)
        if isinstance(error, dict):
            code = safe_error(error.get("code", ""), secret)
            message = safe_error(error.get("message", message), secret)
    except Exception:
        pass
    hint = ""
    if status in (400, 422):
        hint = "；可检查‘输出格式’与思考模式兼容性、模型输出长度及服务地址"
    raise ModelError(f"模型接口 HTTP {status}" + (f"（{code}）" if code else "") + f"：{message}{hint}",
                     fatal=status in (400, 401, 402, 403, 404, 422), status_code=status, code=code)


def embedding_fingerprint(s=None):
    """Compatibility wrapper; new indexes use independent retrieval configuration."""
    from . import retrieval_services
    return retrieval_services.fingerprint()


def embed(texts):
    from . import retrieval_services
    return retrieval_services.embed(texts)
