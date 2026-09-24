"""平台配置。全部可通过环境变量覆盖，详见 .env.example。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 存储 ----
    # 每个批次一个子目录，内含上传原件、batch.json 与解析结果。
    data_dir: Path = PROJECT_ROOT / "data" / "batches"

    # ---- 上传限制 ----
    max_sql_file_bytes: int = 2 * 1024 * 1024
    max_data_file_bytes: int = 512 * 1024 * 1024

    # ---- 数据画像（方案 §3.3） ----
    # 统计最多读取 10 万行；展示与送模型的脱敏样例最多 20 行。
    profile_max_rows: int = 100_000
    profile_max_examples: int = 20
    # 真实数据少于该行数时在结果中标记 sample_size_warning。
    sample_size_warning_threshold: int = 1_000

    # ---- 语义解析（方案 §4.3 / §4.4） ----
    # 小模型置信度低于该值、或存在多个候选时，字段保留待确认状态。
    semantic_confidence_threshold: float = 0.85
    semantic_api_base: str | None = "https://api.deepseek.com/v1"
    semantic_api_key: str | None = "sk-750d1e18ba734f34ad89e0e35c245a10"
    semantic_model: str | None = "deepseek-flash"
    semantic_timeout_seconds: float = 60.0
    # 为 false 时强制使用规则实现，便于本地开发与离线联调。
    semantic_enable_api: bool = True

    # ---- 增强型 Agent ----
    # 为 true 时 Agent 必须成功选择 API 提供方，否则直接失败而不是静默降级。
    agent_require_api: bool = False
    # 单次 Agent 运行最多保留的审查项数量。
    agent_max_review_items: int = 500

    @property
    def use_api_provider(self) -> bool:
        """三项配置齐全且未被显式关闭时才走小模型 API。"""
        return bool(
            self.semantic_enable_api
            and self.semantic_api_base
            and self.semantic_api_key
            and self.semantic_model
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
