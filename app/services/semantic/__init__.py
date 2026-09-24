"""语义补全层：规则实现与小模型实现，通过 SemanticProvider 统一接入。"""

from __future__ import annotations

import logging

from ...config import Settings, get_settings
from .base import (
    FieldContext,
    FieldSemantics,
    ResourceContext,
    ResourceSemantics,
    SemanticProvider,
)
from .openai_compatible import OpenAICompatibleProvider
from .rule_based import RuleBasedProvider, aggregate_resource_tags

logger = logging.getLogger(__name__)

__all__ = [
    "FieldContext",
    "FieldSemantics",
    "ResourceContext",
    "ResourceSemantics",
    "SemanticProvider",
    "RuleBasedProvider",
    "OpenAICompatibleProvider",
    "aggregate_resource_tags",
    "build_provider",
]


def build_provider(settings: Settings | None = None) -> SemanticProvider:
    """按配置选择语义提供方。

    三项 API 配置齐全且未被关闭时走小模型；否则使用规则实现。
    小模型构造失败时同样降级，保证解析流程不中断。
    """
    settings = settings or get_settings()
    if not settings.use_api_provider:
        return RuleBasedProvider()

    try:
        return OpenAICompatibleProvider(
            api_base=settings.semantic_api_base or "",
            api_key=settings.semantic_api_key or "",
            model=settings.semantic_model or "",
            timeout=settings.semantic_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - 配置错误不应让整批解析失败
        logger.warning("小模型提供方初始化失败（%s），降级为规则实现。", exc)
        return RuleBasedProvider()
