"""接口与解析结果的固定结构（方案 §5、§6）。

ResourceDescriptor 是前端唯一依赖的契约；标注为「扩展」的字段是对方案
原文的累加，不影响按原文渲染。所有比例类数值均为 0~1 的小数。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- 任务状态


class BatchStatus(str, Enum):
    """方案 §6 规定的前端轮询状态机。"""

    UPLOADED = "UPLOADED"                    # 文件已落盘，尚未完成配对预览
    PREVIEW_READY = "PREVIEW_READY"          # 配对与字段预览就绪，等待用户填描述并启动
    PARSING = "PARSING"                      # 完整解析进行中
    COMPLETED = "COMPLETED"                  # 全部资源解析成功
    FAILED = "FAILED"                        # 批次级失败，或全部资源均失败


class ResourceStatus(str, Enum):
    PENDING = "PENDING"
    PARSING = "PARSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class SemanticStatus(str, Enum):
    """字段语义的确认状态。

    CONFIRMED：来源明确（人工描述，或模型/规则置信度达标且无歧义）。
    PENDING：模型置信度低于阈值，或存在多个候选，需人工确认（方案 §4.3）。
    """

    CONFIRMED = "CONFIRMED"
    PENDING = "PENDING"


class AgentRunStatus(str, Enum):
    """增强型 Agent 的运行状态。"""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class AgentReviewSeverity(str, Enum):
    """质量审查项严重程度。"""

    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


# ---------------------------------------------------------------- 结果结构


class JoinInfo(BaseModel):
    """一条表关联（方案 §4.1、§5）。"""

    type: str = Field(description="JOIN 类型，如 LEFT、INNER、RIGHT、FULL")
    left_field: str | None = Field(default=None, description="左表关联字段，如 c.id")
    right_field: str | None = Field(default=None, description="右表关联字段，如 o.customer_id")
    condition: str | None = Field(default=None, description="完整 ON 条件原文")


class SourceTableInfo(BaseModel):
    """来源表明细（方案 §4.1 的库名/模式名/表名/别名，扩展字段）。"""

    catalog: str | None = None
    schema_: str | None = Field(default=None, alias="schema")
    table: str
    alias: str | None = None
    qualified_name: str = Field(description="用于展示的限定名，如 dwd.customer")

    model_config = {"populate_by_name": True}


class SqlSummary(BaseModel):
    """SQL 结构解析结果（方案 §4.1）。"""

    dialect: str
    has_aggregation: bool = False
    filters: list[str] = Field(default_factory=list, description="WHERE / HAVING 条件原文")
    joins: list[JoinInfo] = Field(default_factory=list)

    # --- 扩展字段：方案 §4.1 要求但 §5 示例未展示的 AST 细节 ---
    source_tables: list[SourceTableInfo] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    aggregate_functions: list[str] = Field(default_factory=list)
    window_functions: list[str] = Field(default_factory=list)
    time_range_filters: list[str] = Field(default_factory=list, description="识别出的时间范围条件")
    partition_filters: list[str] = Field(default_factory=list, description="识别出的分区条件")
    ctes: list[str] = Field(default_factory=list, description="CTE 名称列表")
    subqueries: int = Field(default=0, description="子查询数量")
    parse_warnings: list[str] = Field(default_factory=list)


class FieldStatistics(BaseModel):
    """字段级数据画像（方案 §4.2）。按字段类型填充对应分组，未涉及的保持 None。"""

    # 通用
    physical_type: str | None = None
    row_count: int | None = None
    null_count: int | None = None
    null_rate: float | None = None
    distinct_count: int | None = None
    distinct_rate: float | None = None

    # 数值
    min: float | None = None
    max: float | None = None
    mean: float | None = None
    median: float | None = None
    quantiles: dict[str, float] | None = Field(default=None, description="如 {'p25':..,'p50':..,'p75':..}")

    # 日期时间
    datetime_min: str | None = None
    datetime_max: str | None = None
    time_span_days: float | None = None

    # 文本
    max_length: int | None = None
    avg_length: float | None = None
    top_values: list[dict[str, Any]] | None = Field(default=None, description="高频值，如 [{'value':'A','count':12,'rate':0.3}]")
    enum_candidates: list[str] | None = Field(default=None, description="枚举候选值，仅在小基数时给出")

    # 标识字段
    is_identifier_candidate: bool = False
    duplicate_rate: float | None = None
    format_pattern: str | None = Field(default=None, description="格式模式，如 'A-99-999'")


class ResourceProfile(BaseModel):
    """数据文件整体画像（方案 §5 的 profile 节点）。"""

    row_count: int = 0
    sampled_row_count: int = 0
    sample_size_warning: bool = Field(default=False, description="真实数据少于 1000 行时为 true")
    data_file: str | None = Field(default=None, description="扩展：配对到的数据文件名")
    column_count: int | None = Field(default=None, description="扩展：数据文件列数")
    unmatched_columns: list[str] = Field(default_factory=list, description="扩展：数据文件中 SQL 未输出的列")
    missing_columns: list[str] = Field(default_factory=list, description="扩展：SQL 输出但数据文件缺失的列")


class FieldDescriptor(BaseModel):
    """输出字段描述（方案 §5 的 fields[] 元素）。"""

    name: str
    display_name: str | None = None
    data_type: str | None = None
    description: str | None = None
    source_expression: str | None = Field(default=None, description="该字段对应的 SQL 表达式原文")
    source_fields: list[str] = Field(default_factory=list, description="血缘：上游字段限定名列表")
    statistics: FieldStatistics = Field(default_factory=FieldStatistics)
    examples: list[str] = Field(default_factory=list, description="已脱敏样例，最多 20 个")
    semantic_confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    # --- 扩展字段 ---
    semantic_status: SemanticStatus = SemanticStatus.CONFIRMED
    semantic_candidates: list[str] = Field(
        default_factory=list,
        description="语义命中存在歧义时的候选名称",
    )
    business_type: str | None = None
    tags: list[str] = Field(default_factory=list)
    unit: str | None = None
    description_source: Literal["user", "model", "rule", "none"] = Field(
        default="none", description="description 的来源，人工描述始终优先"
    )


class ResourceDescriptor(BaseModel):
    """一份 SQL 对应的完整解析结果（方案 §5）。前端仅依赖此结构渲染资源详情。"""

    resource_id: str
    resource_code: str
    name: str | None = None
    description: str | None = None
    source_tables: list[str] = Field(default_factory=list, description="限定表名列表，如 ['dwd.customer']")
    sql_summary: SqlSummary
    profile: ResourceProfile = Field(default_factory=ResourceProfile)
    tags: list[str] = Field(default_factory=list)
    fields: list[FieldDescriptor] = Field(default_factory=list)

    # --- 扩展字段 ---
    sql_file: str | None = None
    generated_at: datetime | None = None
    semantic_provider: str | None = Field(default=None, description="实际使用的语义提供方名称")


# ---------------------------------------------------------------- 批次与接口


class ResourcePreview(BaseModel):
    """上传后立即返回的字段预览（方案 §3.1）。"""

    resource_code: str
    sql_file: str
    data_file: str | None = Field(default=None, description="自动配对到的数据文件，None 表示未配对")
    matched_by: Literal["auto", "manual", "none"] = "none"
    output_fields: list[str] = Field(default_factory=list, description="SQL 输出字段名")
    parse_error: str | None = Field(default=None, description="SQL 解析失败时的原因，此时 output_fields 为空")


class BatchCreatedResponse(BaseModel):
    batch_id: str
    status: BatchStatus
    sql_dialect: str
    resources: list[ResourcePreview] = Field(default_factory=list)
    unmatched_data_files: list[str] = Field(default_factory=list, description="未配对到任何 SQL 的数据文件")
    unmatched_sql_files: list[str] = Field(default_factory=list, description="未配对到数据文件的 SQL")


class StartParsingRequest(BaseModel):
    """启动完整解析（方案 §3.2）。键均为 SQL 文件名。"""

    resource_descriptions: dict[str, str] = Field(
        default_factory=dict, description="资源描述，如 {'customer_profile.sql': '...'}"
    )
    field_descriptions: dict[str, dict[str, str]] = Field(
        default_factory=dict, description="字段属性描述，如 {'customer_profile.sql': {'customer_id': '...'}}"
    )
    mappings: dict[str, str] | None = Field(
        default=None, description="启动前修正配对关系，值为数据文件名；值为空串表示解除配对"
    )


class AgentRunRequest(StartParsingRequest):
    """启动增强型 Agent。"""

    require_api: bool | None = Field(
        default=None,
        description="是否要求必须使用模型 API；为空时读取 AGENT_REQUIRE_API 配置",
    )


class AgentReviewItem(BaseModel):
    """需要人工确认或关注的问题。"""

    review_id: str
    severity: AgentReviewSeverity
    code: str
    message: str
    resource_code: str | None = None
    field_name: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class AgentEvent(BaseModel):
    """Agent 执行轨迹。只记录动作和结果摘要，不保存模型内部思维链。"""

    event_id: str
    timestamp: datetime
    event_type: str
    step: str
    message: str
    resource_code: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class AgentRunResponse(BaseModel):
    """增强型 Agent 当前状态。"""

    agent_run_id: str
    batch_id: str
    status: AgentRunStatus
    provider: str | None = None
    model: str | None = None
    require_api: bool = False
    current_step: str
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    event_count: int = 0
    review_items: list[AgentReviewItem] = Field(default_factory=list)
    error: str | None = None


class AgentEventsResponse(BaseModel):
    """增强型 Agent 执行事件列表。"""

    agent_run_id: str
    events: list[AgentEvent] = Field(default_factory=list)


class ResourceStatusItem(BaseModel):
    resource_code: str
    sql_file: str
    data_file: str | None = None
    matched_by: Literal["auto", "manual", "none"] = "none"
    status: ResourceStatus
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    field_count: int = 0
    output_fields: list[str] = Field(default_factory=list)
    parse_error: str | None = None
    error: str | None = None


class BatchStatusResponse(BaseModel):
    batch_id: str
    status: BatchStatus
    sql_dialect: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    total_resources: int = 0
    completed_resources: int = 0
    failed_resources: int = 0
    resources: list[ResourceStatusItem] = Field(default_factory=list)
    error: str | None = Field(default=None, description="批次级失败原因")


class BatchResultsResponse(BaseModel):
    batch_id: str
    status: BatchStatus
    results: list[ResourceDescriptor] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    detail: str
