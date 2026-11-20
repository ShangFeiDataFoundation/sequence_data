"""Agent 主导的统一资源解析流程。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ...config import Settings, get_settings
from ...schemas import (
    AgentAttribute,
    AgentLLMResult,
    AgentParseResult,
    AgentRunStatus,
    AgentSchemaTable,
    AgentToolTrace,
    ResourceColumnProfile,
    ResourceProcessing,
    ResourceSchemaProfile,
    StatisticalResourceDescriptor,
    UnifiedResourceClassification,
    UnifiedResourceQuality,
    UnifiedResourceReport,
    UnifiedResourceSource,
)
from ...storage import new_id, now
from .. import profiler
from ..resource_analysis import analyze_resource_file
from ..resource_analysis.preprocessing import ResourceInputError
from .llm import LLMAnalysisClient, LLMResponseError, build_llm_client
from .schema_parser import parse_schema_file
from .storage import AgentRunStore

logger = logging.getLogger(__name__)


@dataclass
class ResourceAnalysis:
    file_info: dict[str, Any]
    classification: UnifiedResourceClassification
    schema_profile: ResourceSchemaProfile
    quality: UnifiedResourceQuality
    statistics: dict[str, Any]
    schema_tables: list[AgentSchemaTable] = field(default_factory=list)
    resource: StatisticalResourceDescriptor | None = None
    entity_fields: list[str] = field(default_factory=list)
    metric_fields: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class ParsingAgent:
    """为每个文件或数据库表分别生成统一 JSON 的解析 Agent。"""

    name = "unified_resource_agent"
    tools = (
        "scan_input_resources",
        "classify_resource",
        "parse_resource",
        "invoke_resource_llm_analysis",
        "build_unified_resource_json",
        "link_schema_and_metrics",
    )

    def __init__(
        self,
        store: AgentRunStore,
        settings: Settings | None = None,
    ) -> None:
        self.store = store
        self.settings = settings or get_settings()

    def run(self, run_id: str) -> None:
        record = self.store.load(run_id)
        input_files = record.get("input_files", [])
        self.store.update(
            run_id,
            status=AgentRunStatus.RUNNING.value,
            current_step="scan_input_resources",
            started_at=now().isoformat(),
        )
        trace = [
            AgentToolTrace(
                tool_name="scan_input_resources",
                status="completed",
                summary=f"扫描到 {len(input_files)} 个待解析资源。",
            )
        ]
        llm_client: LLMAnalysisClient | None = None
        try:
            schema_tables, schema_warnings = self._parse_schemas(run_id, record, trace)
            llm_client = build_llm_client(self.settings)
            if llm_client is None and self.settings.llm_required:
                raise RuntimeError(
                    "LLM 未配置，无法为每个资源生成统一 JSON。"
                    "请配置 LLM_API_BASE、LLM_API_KEY 和 LLM_MODEL。"
                )

            reports: list[UnifiedResourceReport] = []
            relationships: list[dict[str, Any]] = []
            total = max(len(input_files), 1)
            for index, file_info in enumerate(input_files):
                relative_path = file_info.get("relative_path") or file_info["name"]
                self.store.update(
                    run_id,
                    current_step=f"parse_resource:{relative_path}",
                    progress=0.12 + 0.72 * (index / total),
                )
                analysis = self._analyze_resource(
                    run_id=run_id,
                    file_info=file_info,
                    schema_tables=schema_tables,
                    schema_warnings=schema_warnings,
                )
                payload = self._build_resource_llm_payload(
                    analysis,
                    all_schema_tables=schema_tables,
                )
                llm_status = "completed"
                if llm_client is None:
                    llm_result = self._fallback_llm_result(analysis)
                else:
                    self.store.update(
                        run_id,
                        current_step=f"invoke_resource_llm_analysis:{relative_path}",
                        progress=0.5 + 0.35 * (index / total),
                    )
                    try:
                        llm_result = llm_client.analyze(payload)
                    except LLMResponseError as exc:
                        warning = (
                            "模型语义生成失败，已保留确定性解析结果："
                            f"{exc}"
                        )
                        analysis.warnings.append(warning)
                        llm_result = self._fallback_llm_result(
                            analysis,
                            reason=warning,
                        )
                        llm_status = "degraded"
                        logger.warning(
                            "资源模型分析降级: %s: %s",
                            relative_path,
                            exc,
                        )

                resource_relationships = self._related_links(
                    analysis,
                    schema_tables=schema_tables,
                    relationships=relationships,
                )
                report = self._build_unified_report(
                    analysis=analysis,
                    llm_result=llm_result,
                    relationships=resource_relationships,
                )
                self.store.save_resource(
                    run_id,
                    report.resource_id,
                    report.model_dump(mode="json"),
                )
                reports.append(report)
                trace.extend(
                    [
                        AgentToolTrace(
                            tool_name="classify_resource",
                            status="completed",
                            summary=(
                                f"{relative_path}: "
                                f"{analysis.classification.data_kind}。"
                            ),
                        ),
                        AgentToolTrace(
                            tool_name="invoke_resource_llm_analysis",
                            status=llm_status,
                            summary=(
                                f"{relative_path}: "
                                f"{llm_result.model or 'fallback'} 生成 "
                                f"{len(llm_result.attributes)} 个属性。"
                            ),
                        ),
                    ]
                )

            result = AgentParseResult(
                run_id=run_id,
                status=AgentRunStatus.COMPLETED,
                input_files=input_files,
                schema_tables=schema_tables,
                reports=reports,
                relationships=relationships,
                tool_trace=trace,
                generated_at=now(),
            )
            self.store.save_result(run_id, result.model_dump(mode="json"))
            self.store.update(
                run_id,
                status=AgentRunStatus.COMPLETED.value,
                current_step="completed",
                progress=1.0,
                report_count=len(reports),
                finished_at=now().isoformat(),
                tool_trace=[item.model_dump(mode="json") for item in trace],
            )
        except Exception as exc:
            logger.exception("Agent 解析失败: %s", run_id)
            self.store.update(
                run_id,
                status=AgentRunStatus.FAILED.value,
                current_step="failed",
                finished_at=now().isoformat(),
                error=str(exc),
                tool_trace=[item.model_dump(mode="json") for item in trace],
            )
        finally:
            if llm_client is not None:
                llm_client.close()

    def _parse_schemas(
        self,
        run_id: str,
        record: dict[str, Any],
        trace: list[AgentToolTrace],
    ) -> tuple[list[AgentSchemaTable], dict[str, list[str]]]:
        tables: list[AgentSchemaTable] = []
        warnings: dict[str, list[str]] = {}
        schema_files = [
            item for item in record.get("input_files", []) if item["role"] == "schema"
        ]
        for file_info in schema_files:
            path = self.store.upload_path(run_id, file_info["file_id"], file_info["name"])
            relative_path = file_info.get("relative_path") or file_info["name"]
            try:
                parsed_tables = parse_schema_file(
                    path,
                    dialect=record.get("sql_dialect", "mysql"),
                )
                for table in parsed_tables:
                    table.source_file = relative_path
                tables.extend(parsed_tables)
            except Exception as exc:  # noqa: BLE001
                message = f"表结构解析失败: {exc}"
                warnings.setdefault(relative_path, []).append(message)
                trace.append(
                    AgentToolTrace(
                        tool_name="parse_resource",
                        status="failed",
                        summary=f"{relative_path}: {message}",
                    )
                )
        trace.append(
            AgentToolTrace(
                tool_name="parse_resource",
                status="completed",
                summary=f"从结构文件解析出 {len(tables)} 张表。",
            )
        )
        return tables, warnings

    def _analyze_resource(
        self,
        *,
        run_id: str,
        file_info: dict[str, Any],
        schema_tables: list[AgentSchemaTable],
        schema_warnings: dict[str, list[str]],
    ) -> ResourceAnalysis:
        path = self.store.upload_path(run_id, file_info["file_id"], file_info["name"])
        relative_path = file_info.get("relative_path") or file_info["name"]
        associated_schema = [
            table for table in schema_tables if table.source_file == relative_path
        ]
        warnings = list(schema_warnings.get(relative_path, []))

        if file_info["role"] == "schema":
            columns = [
                ResourceColumnProfile(
                    name=column.name,
                    data_type=column.data_type,
                    semantic_type="schema_field",
                    nullable=column.nullable,
                    is_primary_key=column.is_primary_key,
                    description=column.description,
                )
                for table in associated_schema
                for column in table.columns
            ]
            return ResourceAnalysis(
                file_info=file_info,
                classification=UnifiedResourceClassification(
                    data_kind="schema",
                    confidence=0.98 if associated_schema else 0.55,
                    reason=(
                        f"解析到 {len(associated_schema)} 张表的结构定义。"
                        if associated_schema
                        else "文件被识别为表结构，但未解析到有效表定义。"
                    ),
                ),
                schema_profile=ResourceSchemaProfile(
                    row_count=None,
                    column_count=len(columns),
                    columns=columns,
                ),
                quality=UnifiedResourceQuality(issues=warnings),
                statistics={
                    "schema_tables": [
                        table.model_dump(mode="json") for table in associated_schema
                    ],
                },
                schema_tables=associated_schema,
                warnings=warnings,
            )

        profile = profiler.profile_file(path, self.settings)
        profile_warnings = [profile.error] if profile.error else []
        warnings.extend(item for item in profile_warnings if item not in warnings)
        field_profiles = self._field_profiles(profile)
        if not field_profiles and file_info.get("column_info"):
            field_profiles = [
                ResourceColumnProfile(
                    name=str(column.get("name") or "unnamed"),
                    data_type=column.get("data_type"),
                    semantic_type=None,
                    nullable=column.get("nullable"),
                    is_primary_key=bool(column.get("is_primary_key")),
                    is_identifier_candidate=False,
                    description=column.get("description"),
                )
                for column in file_info["column_info"]
            ]

        descriptor: StatisticalResourceDescriptor | None = None
        classification = UnifiedResourceClassification(
            data_kind="structured_table",
            confidence=0.86,
            reason="未形成可分析的时间轴与数值指标，按结构化数据表解析。",
        )
        if file_info["role"] == "timeseries":
            try:
                descriptor = analyze_resource_file(
                    path,
                    source_name=relative_path,
                    time_field=file_info.get("time_field"),
                    settings=self.settings,
                )
                if file_info.get("source_type") == "database":
                    descriptor.time_axis.timezone = "database_local"
                classification = UnifiedResourceClassification(
                    data_kind="time_series",
                    confidence=0.98,
                    reason=(
                        f"识别到时间字段 {descriptor.time_axis.timestamp_field}，"
                        f"并提取 {len(descriptor.features)} 组统计特征。"
                    ),
                )
            except (ResourceInputError, ValueError) as exc:
                warning = f"时序解析降级为结构化表: {exc}"
                if warning not in warnings:
                    warnings.append(warning)

        if descriptor is not None:
            statistics = {
                "layout": descriptor.layout,
                "field_statistics": self._field_statistics(profile),
                "metric_summaries": self._metric_summaries(descriptor),
                "chart_series": self._chart_series(descriptor),
                "correlations": [
                    item.model_dump(mode="json") for item in descriptor.correlations
                ][:20],
            }
            quality = UnifiedResourceQuality(
                completeness=descriptor.quality.completeness,
                null_value_count=sum(
                    item.statistics.missing_count for item in descriptor.features
                ),
                issue_count=len(descriptor.quality.issues),
                issues=descriptor.quality.issues,
            )
            row_count = descriptor.quality.row_count
            metric_fields = descriptor.value_fields
            entity_fields = descriptor.entity_fields
        else:
            numeric_types = {"integer", "number"}
            metric_fields = [
                item.name
                for item in field_profiles
                if item.semantic_type in numeric_types
            ]
            entity_fields = []
            null_count = sum(
                int(item_stats.null_count or 0)
                for item_stats in profile.statistics.values()
            )
            statistics = {
                "layout": "structured_table",
                "field_statistics": self._field_statistics(profile),
                "sampled_row_count": profile.sampled_row_count,
                "sample_size_warning": profile.sample_size_warning,
            }
            quality = UnifiedResourceQuality(
                completeness=self._structured_completeness(profile),
                null_value_count=null_count,
                issue_count=len(warnings),
                issues=warnings,
            )
            row_count = profile.row_count or None

        return ResourceAnalysis(
            file_info=file_info,
            classification=classification,
            schema_profile=ResourceSchemaProfile(
                row_count=row_count,
                column_count=len(field_profiles),
                columns=field_profiles,
            ),
            quality=quality,
            statistics=statistics,
            schema_tables=associated_schema,
            resource=descriptor,
            entity_fields=entity_fields,
            metric_fields=metric_fields,
            warnings=warnings,
        )

    @staticmethod
    def _field_profiles(profile: profiler.ProfileResult) -> list[ResourceColumnProfile]:
        return [
            ResourceColumnProfile(
                name=column,
                data_type=(
                    profile.statistics[column].physical_type
                    if column in profile.statistics
                    else None
                ),
                semantic_type=(
                    profile.statistics[column].physical_type
                    if column in profile.statistics
                    else None
                ),
                nullable=(
                    bool(profile.statistics[column].null_count)
                    if column in profile.statistics
                    else None
                ),
                is_primary_key=False,
                is_identifier_candidate=(
                    profile.statistics[column].is_identifier_candidate
                    if column in profile.statistics
                    else False
                ),
            )
            for column in profile.columns
        ]

    @staticmethod
    def _field_statistics(profile: profiler.ProfileResult) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "physical_type": statistics.physical_type,
                "row_count": statistics.row_count,
                "null_rate": statistics.null_rate,
                "distinct_count": statistics.distinct_count,
                "min": statistics.min,
                "max": statistics.max,
                "mean": statistics.mean,
                "median": statistics.median,
                "datetime_min": statistics.datetime_min,
                "datetime_max": statistics.datetime_max,
            }
            for name, statistics in profile.statistics.items()
        ]

    @staticmethod
    def _metric_summaries(
        resource: StatisticalResourceDescriptor,
        limit: int = 60,
    ) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for feature in resource.features[:limit]:
            statistics = feature.statistics
            summaries.append(
                {
                    "entity_ref": feature.entity.entity_key,
                    "metric_id": feature.metric_id,
                    "mean": statistics.mean,
                    "median": statistics.median,
                    "std": statistics.std,
                    "min": statistics.minimum,
                    "max": statistics.maximum,
                    "q1": statistics.q1,
                    "q3": statistics.q3,
                    "missing_rate": statistics.missing_rate,
                    "trend": (
                        {
                            "direction": feature.trend.direction,
                            "normalized_slope": feature.trend.normalized_slope,
                            "strength": feature.trend.strength,
                        }
                        if feature.trend
                        else None
                    ),
                    "seasonality_labels": [
                        item.period_label for item in feature.seasonalities[:3]
                    ],
                    "anomaly_count": len(feature.anomalies),
                    "anomalies": [
                        {
                            "start_time": item.start_time.isoformat(),
                            "end_time": item.end_time.isoformat(),
                            "score": item.score,
                            "observed_value": item.observed_value,
                            "expected_value": item.expected_value,
                        }
                        for item in feature.anomalies[:3]
                    ],
                }
            )
        return summaries

    @staticmethod
    def _chart_series(
        resource: StatisticalResourceDescriptor,
        *,
        max_series: int = 8,
        max_points: int = 120,
    ) -> list[dict[str, Any]]:
        charts: list[dict[str, Any]] = []
        for chart in resource.charts[:max_series]:
            points = chart.points
            step = max(1, (len(points) + max_points - 1) // max_points)
            sampled = points[::step]
            if points and (not sampled or sampled[-1] != points[-1]):
                sampled = [*sampled, points[-1]]
            charts.append(
                {
                    "label": chart.label,
                    "entity_ref": chart.entity_ref,
                    "metric_id": chart.metric_id,
                    "points": [
                        {
                            "timestamp": point.timestamp.isoformat(),
                            "value": point.value,
                        }
                        for point in sampled
                    ],
                }
            )
        return charts

    @staticmethod
    def _structured_completeness(profile: profiler.ProfileResult) -> float | None:
        if not profile.statistics:
            return None
        total = sum(int(item.row_count or 0) for item in profile.statistics.values())
        nulls = sum(int(item.null_count or 0) for item in profile.statistics.values())
        return round(1 - nulls / total, 6) if total else None

    @staticmethod
    def _build_resource_llm_payload(
        analysis: ResourceAnalysis,
        *,
        all_schema_tables: list[AgentSchemaTable],
    ) -> dict[str, Any]:
        info = analysis.file_info
        payload: dict[str, Any] = {
            "task": "为当前单一资源生成统一结构化 JSON 的语义部分",
            "resource": {
                "resource_name": info["name"],
                "display_name": info.get("display_name") or info.get("relative_path"),
                "source_type": info.get("source_type", "upload"),
                "relative_path": info.get("relative_path") or info["name"],
                "database_name": info.get("database_name"),
                "schema_name": info.get("schema_name"),
                "table_name": info.get("table_name"),
                "selected_time_range": {
                    "start": info.get("time_start"),
                    "end": info.get("time_end"),
                    "max_rows": info.get("max_rows"),
                },
            },
            "classification": analysis.classification.model_dump(mode="json"),
            "schema_profile": analysis.schema_profile.model_dump(mode="json"),
            "quality": analysis.quality.model_dump(mode="json"),
            "statistics": analysis.statistics,
            "related_schema_tables": [
                table.model_dump(mode="json")
                for table in all_schema_tables[:100]
            ],
            "warnings": analysis.warnings,
        }
        if analysis.resource is not None:
            payload["time_series"] = {
                "layout": analysis.resource.layout,
                "time_axis": analysis.resource.time_axis.model_dump(mode="json"),
                "entity_fields": analysis.resource.entity_fields,
                "metric_fields": analysis.resource.value_fields,
            }
        return payload

    @staticmethod
    def _fallback_llm_result(
        analysis: ResourceAnalysis,
        *,
        reason: str | None = None,
    ) -> AgentLLMResult:
        info = analysis.file_info
        resource_name = info.get("display_name") or info["name"]
        value = {
            "data_kind": analysis.classification.data_kind,
            "row_count": analysis.schema_profile.row_count,
            "column_count": analysis.schema_profile.column_count,
            "issue_count": analysis.quality.issue_count,
        }
        return AgentLLMResult(
            summary=reason
            or (
                f"{resource_name} 已完成确定性解析；LLM 未启用，"
                "当前仅返回解析摘要。"
            ),
            overall_status="UNKNOWN",
            attributes=[
                AgentAttribute(
                    attribute_name="resource_parse_summary",
                    value=value,
                    description=reason or analysis.classification.reason,
                    category="解析摘要",
                    confidence=1.0,
                    source_files=[resource_name],
                    source_refs=[info.get("relative_path") or info["name"]],
                    evidence=analysis.quality.issues,
                    model_version="fallback",
                )
            ],
            recommendations=[],
            provider="fallback",
            generated_at=now(),
        )

    def _build_unified_report(
        self,
        *,
        analysis: ResourceAnalysis,
        llm_result: AgentLLMResult,
        relationships: list[dict[str, Any]],
    ) -> UnifiedResourceReport:
        info = analysis.file_info
        source_type = info.get("source_type", "upload")
        resource_id = (
            analysis.resource.resource_id
            if analysis.resource is not None
            else new_id("res")
        )
        return UnifiedResourceReport(
            report_id=new_id("report"),
            resource_id=resource_id,
            resource_type=(
                "database_table" if source_type == "database" else "file"
            ),
            resource_name=info["name"],
            display_name=info.get("display_name") or info.get("relative_path") or info["name"],
            source=UnifiedResourceSource(
                source_type=source_type,
                relative_path=info.get("relative_path") or info["name"],
                file_format=info.get("format", ""),
                file_size_bytes=int(info.get("size_bytes") or 0),
                database_type=(
                    info.get("database_type")
                    if source_type == "database"
                    else None
                ),
                database_name=info.get("database_name"),
                schema_name=info.get("schema_name"),
                table_name=info.get("table_name"),
            ),
            classification=analysis.classification,
            schema_profile=analysis.schema_profile,
            time_axis=(
                analysis.resource.time_axis if analysis.resource is not None else None
            ),
            entity_fields=(
                analysis.entity_fields
            ),
            metric_fields=analysis.metric_fields,
            quality=analysis.quality,
            statistics=analysis.statistics,
            attributes=llm_result.attributes,
            summary=llm_result.summary,
            overall_status=llm_result.overall_status,
            recommendations=llm_result.recommendations,
            relationships=relationships,
            processing=ResourceProcessing(
                status="completed",
                warnings=analysis.warnings,
            ),
            generated_at=now(),
        )

    @staticmethod
    def _related_links(
        analysis: ResourceAnalysis,
        *,
        schema_tables: list[AgentSchemaTable],
        relationships: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        source = analysis.file_info.get("relative_path") or analysis.file_info["name"]
        resource_name = analysis.file_info["name"]
        schema_columns = {
            column.name.lower(): table.table_name
            for table in schema_tables
            for column in table.columns
        }
        metrics = (
            analysis.resource.value_fields
            if analysis.resource is not None
            else [
                column.name
                for column in analysis.schema_profile.columns
                if column.semantic_type in {"integer", "number"}
            ]
        )
        links: list[dict[str, Any]] = []
        for metric in metrics:
            normalized = metric.lower().replace("_", "")
            for column_name, table_name in schema_columns.items():
                if normalized != column_name.replace("_", ""):
                    continue
                link = {
                    "relation_type": "SCHEMA_METRIC_MATCH",
                    "table_name": table_name,
                    "column_name": column_name,
                    "resource_id": (
                        analysis.resource.resource_id
                        if analysis.resource is not None
                        else None
                    ),
                    "resource_name": resource_name,
                    "source_file": source,
                    "metric_ref": metric,
                    "confidence": 1.0,
                }
                links.append(link)
                relationships.append(link)
        return links
