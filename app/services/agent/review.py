"""增强型 Agent 的结果审查规则。"""

from __future__ import annotations

from ...schemas import (
    AgentReviewItem,
    AgentReviewSeverity,
    ResourceDescriptor,
    SemanticStatus,
)
from ...storage import new_id


def build_review_items(
    descriptor: ResourceDescriptor,
    *,
    limit: int = 500,
    offset: int = 0,
) -> list[AgentReviewItem]:
    """从确定性解析结果中提取需要人工确认或关注的问题。"""
    if offset >= limit:
        return []

    items: list[AgentReviewItem] = []

    def add(
        severity: AgentReviewSeverity,
        code: str,
        message: str,
        *,
        field_name: str | None = None,
        evidence: dict | None = None,
    ) -> None:
        if offset + len(items) >= limit:
            return
        items.append(
            AgentReviewItem(
                review_id=new_id("rev"),
                severity=severity,
                code=code,
                message=message,
                resource_code=descriptor.resource_code,
                field_name=field_name,
                evidence=evidence or {},
            )
        )

    summary = descriptor.sql_summary
    profile = descriptor.profile

    for warning in summary.parse_warnings:
        add(
            AgentReviewSeverity.WARNING,
            "SQL_PARSE_WARNING",
            warning,
        )

    if not profile.data_file:
        add(
            AgentReviewSeverity.WARNING,
            "DATA_FILE_UNMATCHED",
            "该 SQL 尚未配对数据文件，数据统计画像为空。",
        )

    if profile.missing_columns:
        add(
            AgentReviewSeverity.ERROR,
            "PROFILE_MISSING_COLUMNS",
            "SQL 输出字段在数据文件中缺失，请检查字段映射或数据表版本。",
            evidence={"columns": profile.missing_columns},
        )

    if profile.unmatched_columns:
        add(
            AgentReviewSeverity.WARNING,
            "PROFILE_UNMATCHED_COLUMNS",
            "数据文件中存在 SQL 未输出的字段。",
            evidence={"columns": profile.unmatched_columns},
        )

    if profile.sample_size_warning:
        add(
            AgentReviewSeverity.INFO,
            "SMALL_SAMPLE",
            "数据样本少于配置阈值，部分统计结论稳定性有限。",
            evidence={
                "row_count": profile.row_count,
                "sampled_row_count": profile.sampled_row_count,
            },
        )

    for field in descriptor.fields:
        if field.semantic_status != SemanticStatus.PENDING:
            continue
        add(
            AgentReviewSeverity.WARNING,
            "FIELD_SEMANTICS_PENDING",
            "字段语义置信度不足或存在多个候选，需要人工确认。",
            field_name=field.name,
            evidence={
                "candidates": field.semantic_candidates,
                "confidence": field.semantic_confidence,
                "source_expression": field.source_expression,
                "source_fields": field.source_fields,
            },
        )

    return items
