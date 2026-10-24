"""配置加载：YAML 为基线，环境变量覆盖。

优先级：构造函数入参 > YAML 文件 > 环境变量 > .env 文件。

环境变量前缀 ``QIANWEN_``，嵌套用双下划线，例如::

    QIANWEN_UPSTREAM__BASE_URL=http://127.0.0.1:8000/v1
    QIANWEN_LIMITS__MAX_CONCURRENT=8
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

DEFAULT_CONFIG_PATH = "configs/gateway.yaml"


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 9000
    log_level: str = "info"


class UpstreamConfig(BaseModel):
    """vLLM OpenAI 兼容服务地址。"""

    base_url: str = "http://127.0.0.1:8000/v1"
    api_key: str = ""
    model: str = "qwen3.8-27b"
    request_timeout_s: float = 600.0
    connect_timeout_s: float = 15.0


class AuthConfig(BaseModel):
    """鉴权方式。

    - ``header``：信任 Open WebUI 转发过来的用户头（要求网关只在内网暴露）
    - ``shared``：所有请求共用一个 API Key，映射到 ``default_user_id``（单人或压测）
    - ``off``：不鉴权，仅本地调试
    """

    mode: Literal["header", "shared", "off"] = "header"
    api_keys: list[str] = Field(default_factory=list)
    admin_token: str = ""
    header_user_id: str = "X-OpenWebUI-User-Id"
    header_user_email: str = "X-OpenWebUI-User-Email"
    header_user_name: str = "X-OpenWebUI-User-Name"
    header_user_role: str = "X-OpenWebUI-User-Role"
    header_chat_id: str = "X-OpenWebUI-Chat-Id"
    default_user_id: str = "anonymous"
    default_user_name: str = "匿名用户"


class HistoryConfig(BaseModel):
    """会话历史落库 + 上下文窗口裁剪。"""

    enabled: bool = True
    max_turns: int = 20
    max_prompt_tokens: int = 24576
    store_assistant: bool = True


class MemoryConfig(BaseModel):
    """跨会话长期记忆。"""

    enabled: bool = True
    inject_top_k: int = 5
    min_score: float = 0.12
    max_inject_tokens: int = 600
    extract_mode: Literal["llm", "heuristic", "off"] = "llm"
    extract_max_memories: int = 3
    extract_min_chars: int = 8
    max_per_user: int = 500
    dedup_similarity: float = 0.9
    recency_half_life_days: float = 180.0


class LimitsConfig(BaseModel):
    max_concurrent: int = 4
    queue_timeout_s: float = 60.0
    rpm_per_user: int = 60
    daily_token_quota: int = 0  # 0 表示不限额


class StorageConfig(BaseModel):
    db_path: str = "data/gateway.db"


class _YamlSource(PydanticBaseSettingsSource):
    """把 YAML 文件内容作为一层设置来源。"""

    def __init__(self, settings_cls: type[BaseSettings], path: str | None = None) -> None:
        super().__init__(settings_cls)
        self._path = path

    def get_field_value(self, field, field_name):
        return None, field_name, False

    def __call__(self) -> dict:
        path = Path(self._path or os.environ.get("QIANWEN_CONFIG") or DEFAULT_CONFIG_PATH)
        if not path.is_file():
            return {}
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise TypeError(f"配置文件 {path} 顶层必须是 mapping")
        return data


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="QIANWEN_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    server: ServerConfig = Field(default_factory=ServerConfig)
    upstream: UpstreamConfig = Field(default_factory=UpstreamConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # 优先级：显式传参 > 环境变量 > .env > configs/gateway.yaml > 文件密钥。
        # yaml 只是"基线默认值"，环境变量必须能覆盖它 —— 否则运维改了 .env 却不生效，
        # 排障时非常难发现（历史真实故障：GATEWAY_MAX_CONCURRENT 改不动）。
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _YamlSource(settings_cls),
            file_secret_settings,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
