"""解析流水线：AST 结构 + 数据画像 + 语义补全，组装为方案 §5 的固定 JSON。

合并优先级（方案 §4.4）在这里集中实现：
1. 人工填写的资源描述与字段属性描述永远优先，模型不得覆盖；
2. 模型只补充缺失的显示名、标签、单位；
3. 字段集合完全由 SQL 决定，模型无法新增或删除字段。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings, get_settings
from ..schemas import (
    BatchStatus,
    FieldDescriptor,
    FieldStatistics,
    ResourceDescriptor,
    ResourcePreview,
    ResourceProfile,
    ResourceStatus,
    SemanticStatus,
)
from ..storage import BatchStore, build_resource_descriptor, now, safe_filename
from . import profiler
from .pairing import PairingResult, pair_files, resource_code_of
from .semantic import (
    FieldContext,
    ResourceContext,
    SemanticProvider,
    build_provider,
)
from .semantic.rule_based import aggregate_resource_tags
from .sql_parser import OutputField, SqlParseResult, parse_sql

logger = logging.getLogger(__name__)


@dataclass
class ResourceInput:
    """一个待解析资源的上传原件位置。"""

    resource_code: str
    sql_file: str
    sql_path: Path
    data_file: str | None
    data_path: Path | None


# ---------------------------------------------------------------- 预览


def build_previews(
    store: BatchStore,
    batch_id: str,
    sql_files: list[str],
    data_files: list[str],
    dialect: str,
    mappings: dict[str, str] | None = None,
    extra_mappings: dict[str, str] | None = None,
) -> tuple[list[ResourcePreview], PairingResult]:
    """上传后立即返回配对结果与字段预览（方案 §3.1）。"""
    merged_mappings = {**(mappings or {}), **(extra_mappings or {})}
    pairing = pair_files(sql_files, data_files, merged_mappings)

    previews: list[ResourcePreview] = []
    uploads = store.uploads_dir(batch_id)

    for sql_file in sql_files:
        code = resource_code_of(sql_file)
        data_file = pairing.data_file_for(code)
        matched_by = "auto" if code in pairing.auto_matched else ("manual" if data_file else "none")

        preview = ResourcePreview(
            resource_code=code,
            sql_file=sql_file,
            data_file=data_file,
            matched_by=matched_by,  # type: ignore[arg-type]
        )

        sql_path = uploads / safe_filename(sql_file)
        if not sql_path.exists():
            preview.parse_error = "SQL 文件缺失。"
            previews.append(preview)
            continue

        try:
            result = parse_sql(sql_path.read_text(encoding="utf-8", errors="replace"), dialect)
        except OSError as exc:
            preview.parse_error = f"读取 SQL 文件失败: {exc}"
            previews.append(preview)
            continue

        if result.ok:
            preview.output_fields = [f.name for f in result.output_fields]
        else:
            preview.parse_error = result.error
        previews.append(preview)

    return previews, pairing


# ---------------------------------------------------------------- 完整解析


def run_batch(batch_id: str, store: BatchStore, settings: Settings | None = None) -> None:
    """执行整批解析。由后台任务调用，逐资源推进并持续落盘进度。"""
    settings = settings or get_settings()
    provider = build_provider(settings)
    provider_name = provider.name

    record = store.update(batch_id, status=BatchStatus.PARSING.value, started_at=now().isoformat())
    resource_descriptions: dict[str, str] = record.get("resource_descriptions") or {}
    field_descriptions: dict[str, dict[str, str]] = record.get("field_descriptions") or {}

    try:
        for resource in list(record["resources"]):
            code = resource["resource_code"]
            store.update_resource(batch_id, code, status=ResourceStatus.PARSING.value, progress=0.1)

            try:
                descriptor = parse_resource(
                    batch_id=batch_id,
                    store=store,
                    resource=resource,
                    dialect=record["sql_dialect"],
                    resource_description=resource_descriptions.get(code)
                    or resource_descriptions.get(resource["sql_file"]),
                    field_descriptions=_field_descriptions_for(resource, field_descriptions),
                    provider=provider,
                    settings=settings,
                )
            except Exception as exc:
                logger.exception("资源解析失败: %s", code)
                store.update_resource(
                    batch_id, code,
                    status=ResourceStatus.FAILED.value, progress=1.0, error=str(exc),
                )
                continue

            # 与 FastAPI 的响应序列化保持一致（同样按别名输出），
            # 否则落盘文件会出现 schema_ 而接口返回 schema。
            store.save_result(batch_id, code, descriptor.model_dump(mode="json", by_alias=True))
            store.update_resource(
                batch_id, code,
                status=ResourceStatus.COMPLETED.value,
                progress=1.0,
                field_count=len(descriptor.fields),
                error=None,
            )
    finally:
        provider.close()

    finalize_batch(batch_id, store, provider_name)


def _field_descriptions_for(
    resource: dict, field_descriptions: dict[str, dict[str, str]]
) -> dict[str, str]:
    """字段描述允许按 SQL 文件名或资源编码两种键提交。"""
    return (
        field_descriptions.get(resource["resource_code"])
        or field_descriptions.get(resource["sql_file"])
        or {}
    )


def finalize_batch(batch_id: str, store: BatchStore, provider_name: str) -> None:
    """根据资源状态落定批次最终状态。"""
    record = store.load(batch_id)
    resources = record["resources"]
    failed = [r for r in resources if r["status"] == ResourceStatus.FAILED.value]
    completed = [r for r in resources if r["status"] == ResourceStatus.COMPLETED.value]

    # 全部资源都失败才算批次失败；部分成功仍可交付已完成的结果。
    status = BatchStatus.FAILED if (failed and not completed) else BatchStatus.COMPLETED
    store.update(
        batch_id,
        status=status.value,
        finished_at=now().isoformat(),
        provider=provider_name,
    )


def parse_resource(
    *,
    batch_id: str,
    store: BatchStore,
    resource: dict,
    dialect: str,
    resource_description: str | None,
    field_descriptions: dict[str, str],
    provider: SemanticProvider,
    settings: Settings,
) -> ResourceDescriptor:
    """解析单个资源，产出方案 §5 的固定 JSON。"""
    code = resource["resource_code"]
    uploads = store.uploads_dir(batch_id)
    sql_path = uploads / safe_filename(resource["sql_file"])
    sql_text = sql_path.read_text(encoding="utf-8", errors="replace")

    parse_result = parse_sql(sql_text, dialect)
    if not parse_result.ok or parse_result.summary is None:
        raise ValueError(parse_result.error or "SQL 解析失败。")

    store.update_resource(batch_id, code, progress=0.35)

    # --- 数据画像 ---
    profile_result: profiler.ProfileResult | None = None
    if resource.get("data_file"):
        data_path = uploads / safe_filename(resource["data_file"])
        profile_result = profiler.profile_file(data_path, settings)
        if profile_result.error:
            logger.warning("资源 %s 数据画像失败: %s", code, profile_result.error)

    store.update_resource(batch_id, code, progress=0.55)

    # --- 字段组装 ---
    fields = _build_fields(
        parse_result=parse_result,
        profile_result=profile_result,
        field_descriptions=field_descriptions,
        resource_description=resource_description,
        resource_code=code,
        provider=provider,
        settings=settings,
    )

    store.update_resource(batch_id, code, progress=0.85)

    # --- 资源级语义 ---
    source_tables = [t.qualified_name for t in parse_result.summary.source_tables]
    resource_semantics = provider.analyze_resource(
        ResourceContext(
            resource_code=code,
            resource_description=resource_description,
            source_tables=source_tables,
            field_names=[f.name for f in fields],
            has_aggregation=parse_result.summary.has_aggregation,
            row_count=profile_result.row_count if profile_result else 0,
        )
    )

    names = [f.name for f in fields]
    profile = ResourceProfile(
        row_count=profile_result.row_count if profile_result else 0,
        sampled_row_count=profile_result.sampled_row_count if profile_result else 0,
        sample_size_warning=profile_result.sample_size_warning if profile_result else False,
        data_file=resource.get("data_file"),
        column_count=profile_result.column_count if profile_result else None,
        unmatched_columns=_unmatched_columns(profile_result, names),
        missing_columns=_missing_columns(profile_result, names),
    )

    return ResourceDescriptor.model_validate(
        build_resource_descriptor(
            resource_code=code,
            sql_file=resource["sql_file"],
            name=resource_semantics.name or code,
            description=resource_description or resource_semantics.description,
            source_tables=source_tables,
            sql_summary=parse_result.summary,
            profile=profile,
            tags=_merge_tags(fields, resource_semantics.tags),
            fields=[f.model_dump(mode="json") for f in fields],
            provider_name=provider.name,
        )
    )


def _build_fields(
    *,
    parse_result: SqlParseResult,
    profile_result: profiler.ProfileResult | None,
    field_descriptions: dict[str, str],
    resource_description: str | None,
    resource_code: str,
    provider: SemanticProvider,
    settings: Settings,
) -> list[FieldDescriptor]:
    """组装字段列表。

    字段集合严格来自 SQL 输出（SELECT * 时退回数据文件表头），
    模型结果只做属性补全，无法增删字段。
    """
    out_fields: list[OutputField] = parse_result.output_fields

    # SELECT * 无法静态确定字段名时，用数据文件表头兜底。
    if out_fields and all(f.is_star for f in out_fields):
        if profile_result and profile_result.columns:
            out_fields = [OutputField(name=c, expression_sql="*", is_star=False) for c in profile_result.columns]
        else:
            raise ValueError(
                "SQL 使用了 SELECT *，且没有配对的数据文件可提供字段名，无法确定输出字段。"
            )

    fields: list[FieldDescriptor] = []
    for out_field in out_fields:
        stats, examples = _lookup_stats(profile_result, out_field.name)
        user_description = _lookup_description(field_descriptions, out_field.name)

        semantics = provider.analyze_field(
            FieldContext(
                resource_code=resource_code,
                resource_description=resource_description,
                field_name=out_field.name,
                sql_expression=out_field.expression_sql,
                physical_type=stats.physical_type,
                user_description=user_description,
                source_tables=[t.qualified_name for t in (parse_result.summary.source_tables if parse_result.summary else [])],
                source_fields=out_field.source_fields,
                masked_examples=examples,
                statistics=stats.model_dump(exclude_none=True),
            )
        )

        # 人工描述优先；模型只在缺失时补位（方案 §4.4）。
        if user_description:
            description, source = user_description, "user"
        elif semantics.description:
            description, source = semantics.description, (
                "model" if semantics.provider != "rule_based" else "rule"
            )
        else:
            description, source = None, "none"

        ambiguous = bool(semantics.candidates) or semantics.confidence < settings.semantic_confidence_threshold
        # 人工描述已经明确了字段含义，歧义即被消解。
        status = SemanticStatus.PENDING if (ambiguous and not user_description) else SemanticStatus.CONFIRMED

        fields.append(
            FieldDescriptor(
                name=out_field.name,
                display_name=semantics.display_name or _fallback_display_name(out_field.name),
                data_type=stats.physical_type or _infer_type_from_expression(out_field.expression_sql),
                description=description,
                source_expression=out_field.expression_sql,
                source_fields=out_field.source_fields,
                statistics=stats,
                examples=examples,
                semantic_confidence=semantics.confidence,
                semantic_status=status,
                semantic_candidates=semantics.candidates,
                business_type=semantics.business_type,
                tags=semantics.tags,
                unit=semantics.unit,
                description_source=source,  # type: ignore[arg-type]
            )
        )
    return fields


def _lookup_stats(
    profile_result: profiler.ProfileResult | None, field_name: str
) -> tuple[FieldStatistics, list[str]]:
    """按名称匹配画像结果，容忍大小写与下划线差异。"""
    if profile_result is None:
        return FieldStatistics(), []

    key = _match_column(profile_result, field_name)
    if key is None:
        return FieldStatistics(), []
    return profile_result.statistics.get(key, FieldStatistics()), profile_result.examples.get(key, [])


def _lookup_description(descriptions: dict[str, str], field_name: str) -> str | None:
    """字段描述匹配同样容忍大小写差异。"""
    if field_name in descriptions:
        return descriptions[field_name] or None
    lowered = field_name.lower()
    for key, value in descriptions.items():
        if key.lower() == lowered:
            return value or None
    return None


def _match_column(profile_result: profiler.ProfileResult, field_name: str) -> str | None:
    if field_name in profile_result.statistics:
        return field_name
    lowered = field_name.lower()
    for column in profile_result.columns:
        if column.lower() == lowered:
            return column
    normalized = lowered.replace("_", "")
    for column in profile_result.columns:
        if column.lower().replace("_", "") == normalized:
            return column
    return None


def _unmatched_columns(profile_result: profiler.ProfileResult | None, names: list[str]) -> list[str]:
    """数据文件中存在、但 SQL 未输出的列。"""
    if profile_result is None:
        return []
    normalized = {n.lower().replace("_", "") for n in names}
    return [c for c in profile_result.columns if c.lower().replace("_", "") not in normalized]


def _missing_columns(profile_result: profiler.ProfileResult | None, names: list[str]) -> list[str]:
    """SQL 输出、但数据文件中缺失的列。"""
    if profile_result is None:
        return []
    available = {c.lower().replace("_", "") for c in profile_result.columns}
    return [n for n in names if n.lower().replace("_", "") not in available]


def _merge_tags(fields: list[FieldDescriptor], resource_tags: list[str]) -> list[str]:
    """资源标签 = 资源级标签 + 字段标签高频项，去重保序。"""
    merged: list[str] = []
    for tag in list(resource_tags) + aggregate_resource_tags([f.tags for f in fields]):
        if tag and tag not in merged:
            merged.append(tag)
    return merged


def _fallback_display_name(field_name: str) -> str:
    """无语义结果时的显示名兜底：原样返回字段名，不做臆测。"""
    return field_name


def _infer_type_from_expression(expression_sql: str | None) -> str | None:
    """无数据文件时，仅对含义无歧义的聚合函数推断类型。"""
    if not expression_sql:
        return None
    upper = expression_sql.upper()
    if "COUNT(" in upper:
        return "integer"
    if any(fn in upper for fn in ("SUM(", "AVG(", "STDDEV(", "VARIANCE(")):
        return "number"
    return None
