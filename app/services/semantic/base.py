"""小模型/API 抽象接口（方案 §4.4）。

模型入参的边界在这里收口：FieldContext 是唯一的数据载体，它只从画像结果
和 SQL 结构里取值，不含任何原始明细行。新增字段时必须同步确认这一点。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FieldContext:
    """送入语义分析的单个字段上下文（全部为已脱敏/聚合信息）。"""

    resource_code: str
    resource_description: str | None
    field_name: str
    sql_expression: str | None
    physical_type: str | None
    user_description: str | None
    source_tables: list[str] = field(default_factory=list)
    source_fields: list[str] = field(default_factory=list)
    masked_examples: list[str] = field(default_factory=list)
    statistics: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        """构造模型入参，对应方案 §4.4 的输入示例。"""
        profile: dict[str, Any] = {}
        for key in ("null_rate", "distinct_rate", "min", "max", "mean", "median",
                    "datetime_min", "datetime_max", "max_length", "avg_length",
                    "is_identifier_candidate", "format_pattern", "enum_candidates"):
            value = self.statistics.get(key)
            if value is not None:
                profile[key] = value

        field_payload: dict[str, Any] = {
            "name": self.field_name,
            "physical_type": self.physical_type,
        }
        if self.sql_expression:
            field_payload["sql_expression"] = self.sql_expression
        if self.user_description:
            field_payload["user_description"] = self.user_description
        if self.source_tables:
            field_payload["source_tables"] = self.source_tables
        if self.source_fields:
            field_payload["source_fields"] = self.source_fields
        if self.masked_examples:
            profile["masked_examples"] = self.masked_examples

        payload: dict[str, Any] = {
            "resource_code": self.resource_code,
            "field": field_payload,
            "profile": profile,
        }
        if self.resource_description:
            payload["resource_description"] = self.resource_description
        return payload


@dataclass
class ResourceContext:
    """资源级语义分析的上下文。"""

    resource_code: str
    resource_description: str | None
    source_tables: list[str] = field(default_factory=list)
    field_names: list[str] = field(default_factory=list)
    has_aggregation: bool = False
    row_count: int = 0

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "resource_code": self.resource_code,
            "field_names": self.field_names,
            "source_tables": self.source_tables,
            "has_aggregation": self.has_aggregation,
            "row_count": self.row_count,
        }
        if self.resource_description:
            payload["resource_description"] = self.resource_description
        return payload


@dataclass
class FieldSemantics:
    """字段语义补全结果，对应方案 §4.4 的固定返回 JSON。"""

    display_name: str | None = None
    description: str | None = None
    business_type: str | None = None
    tags: list[str] = field(default_factory=list)
    unit: str | None = None
    confidence: float = 0.0
    candidates: list[str] = field(default_factory=list)
    provider: str = "unknown"


@dataclass
class ResourceSemantics:
    """资源级补全结果，只用于补齐缺失的名称与标签。"""

    name: str | None = None
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    confidence: float = 0.0
    provider: str = "unknown"


class SemanticProvider(ABC):
    """语义提供方。实现必须保证：失败可预期、不抛未捕获异常、不臆造字段。"""

    name: str = "base"

    @abstractmethod
    def analyze_field(self, context: FieldContext) -> FieldSemantics:
        """补全单个字段的显示名、描述、业务类型、标签与单位。"""

    @abstractmethod
    def analyze_resource(self, context: ResourceContext) -> ResourceSemantics:
        """补全资源级名称与标签。"""

    def close(self) -> None:
        """释放底层资源，默认无操作。"""
