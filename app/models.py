import datetime as dt
import ipaddress
import socket
from typing import Literal
from urllib.parse import urlsplit
from pydantic import BaseModel, Field, field_validator, model_validator
from .sources import official_url

# Hosts an operator may legitimately point model/embedding/rerank services at
# on the deployment machine; numeric loopback stays blocked so an untrusted
# save cannot probe the host or reach metadata endpoints.
ALLOWED_LOCAL_ENDPOINT_HOSTS = ("localhost", "host.docker.internal", "ollama")


def _resolved_ips(host, port):
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        return [ipaddress.ip_address(info[4][0])
                for info in socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)]
    except socket.gaierror:
        # Unresolvable at save time: the endpoint cannot be operator-verified, so
        # saving is allowed but the residual DNS-rebinding window is documented
        # as a known boundary (PRD §13).
        return []


class CaseInput(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    event_date: dt.date = Field(default_factory=dt.date.today)
    jurisdiction: str = Field(default="全国", min_length=1, max_length=40)


class LawInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    version: str = Field(min_length=1, max_length=100)
    jurisdiction: str = Field(default="全国", min_length=1, max_length=40)
    kind: Literal["法律", "行政法规", "司法解释", "地方性法规", "部门规章"] = "法律"
    source_url: str = Field(default="", max_length=1500)
    source_kind: Literal["file", "paste", "url"] = "paste"
    source_name: str = Field(default="粘贴正文", min_length=1, max_length=255)
    canonical_name: str = Field(default="", max_length=200)
    document_number: str = Field(default="", max_length=100)
    valid_from: dt.date
    valid_to: dt.date | None = None
    text: str = Field(min_length=10, max_length=2_000_000)

    @field_validator("source_url")
    @classmethod
    def source(cls, value):
        return official_url(value) if value.strip() else ""

    @model_validator(mode="after")
    def source_and_dates(self):
        if self.source_kind == "url" and not self.source_url:
            raise ValueError("链接读取方式须保留官网原文链接")
        if not self.source_name.strip():
            raise ValueError("正文来源名称不能为空")
        if self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("失效日须晚于生效日；失效日当天不再纳入此版本")
        return self


class LawURLInput(BaseModel):
    url: str = Field(min_length=1, max_length=1500)

    @field_validator("url")
    @classmethod
    def source(cls, value):
        return official_url(value)


class LawPreviewInput(BaseModel):
    # Splitting is available before users fill in legal metadata. Saving still
    # validates the complete LawInput, including source and effective dates.
    text: str = Field(min_length=10, max_length=2_000_000)


class AskInput(BaseModel):
    text: str = Field(min_length=1, max_length=6000)
    mode: Literal["strict", "extract"] = "strict"
    web_query: str = Field(default="", max_length=180)
    request_id: str = Field(default="", max_length=64)
    online_research: bool | None = None


class MemoryInput(BaseModel):
    text: str = Field(min_length=1, max_length=1200)


class SettingsInput(BaseModel):
    profile: Literal["local", "api"] = "local"
    provider: Literal["none", "ollama", "dashscope", "deepseek", "siliconflow", "custom"] = "none"
    base_url: str = "http://host.docker.internal:11434/v1"
    model: str = Field(default="qwen3:8b", max_length=120)
    api_key: str | None = Field(default=None, max_length=1000)
    verify_model: str = Field(default="", max_length=120)
    embedding_model: str = Field(default="", max_length=120)
    # Legacy field retained for 1.2.x migration; new retrieval settings are independent.
    timeout: int = Field(default=120, ge=10, le=600)
    max_tokens: int = Field(default=4000, ge=1000, le=16000)
    review_days: int = Field(default=180, ge=1, le=3650)
    thinking_mode: Literal["auto", "enabled", "disabled"] = "auto"
    show_reasoning: bool = True
    json_mode: Literal["auto", "strict", "prompt"] = "auto"
    online_research: bool = False
    agent_rounds: int = Field(default=3, ge=1, le=3)
    agent_timeout: int = Field(default=360, ge=30, le=1800)

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value):
        u = urlsplit(value)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError("服务地址格式不正确；使用服务的 /v1 或兼容端点")
        host = u.hostname
        if host not in ALLOWED_LOCAL_ENDPOINT_HOSTS:
            if host in ("169.254.169.254", "metadata.google.internal"):
                raise ValueError("不允许云实例元数据地址")
            for ip in _resolved_ips(host, u.port):
                if ip.version == 6 and ip.ipv4_mapped:
                    ip = ip.ipv4_mapped
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
                    raise ValueError("不允许内网、回环或保留地址；本地模型请使用 localhost、host.docker.internal 或 ollama 服务名")
        return value.rstrip("/")


class SearchSettingsInput(BaseModel):
    provider: Literal["none", "bocha", "searxng"] = "none"
    base_url: str = Field(default="https://api.bochaai.com/v1", max_length=1500)
    api_key: str | None = Field(default=None, max_length=1000)
    timeout: int = Field(default=20, ge=5, le=45)

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value):
        return SettingsInput.endpoint(value)

    @model_validator(mode="after")
    def bocha_endpoint(self):
        if self.provider == "bocha" and self.base_url != "https://api.bochaai.com/v1":
            raise ValueError("博查使用官方固定地址 https://api.bochaai.com/v1")
        return self
