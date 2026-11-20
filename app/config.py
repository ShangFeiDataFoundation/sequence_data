"""统一 Agent 解析流程配置。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

try:
    from .local_secrets import LLM_API_BASE, LLM_API_KEY, LLM_MODEL
except ImportError:  # pragma: no cover - 没有本地私有文件时使用环境变量
    LLM_API_BASE = None
    LLM_API_KEY = None
    LLM_MODEL = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 存储 ----
    agent_runs_dir: Path = PROJECT_ROOT / "data" / "agent_runs"

    # ---- 上传限制 ----
    max_schema_file_bytes: int = 2 * 1024 * 1024
    max_data_file_bytes: int = 512 * 1024 * 1024
    max_folder_file_count: int = 2_000
    max_folder_total_bytes: int = 1024 * 1024 * 1024

    # ---- 数据画像 ----
    profile_max_rows: int = 100_000
    profile_max_examples: int = 20
    sample_size_warning_threshold: int = 1_000

    # ---- 时序统计特征 ----
    timeseries_max_points: int = 200_000
    timeseries_min_points: int = 3
    timeseries_anomaly_zscore: float = 3.5
    timeseries_correlation_threshold: float = 0.6

    # ---- LLM 最终结构化理解 ----
    llm_api_base: str | None = LLM_API_BASE
    llm_api_key: str | None = LLM_API_KEY
    llm_model: str | None = LLM_MODEL
    llm_enable_api: bool = True
    llm_required: bool = True
    llm_timeout_seconds: float = 60.0
    llm_max_output_tokens: int = 8192

    # ---- MySQL 数据源 ----
    database_connection_ttl_seconds: int = 30 * 60
    database_connect_timeout_seconds: int = 8
    database_query_timeout_seconds: int = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
