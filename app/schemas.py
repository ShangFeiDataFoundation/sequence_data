"""当前统一 Agent 流程的固定数据契约。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr


class AgentRunStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class TimeSeriesDecisionStatus(str, Enum):
    AUTO_ACCEPTED = "AUTO_ACCEPTED"
    AUTO_ACCEPTED_WITH_ALTERNATIVES = "AUTO_ACCEPTED_WITH_ALTERNATIVES"
    UNRESOLVED = "UNRESOLVED"
    AUTO_REJECTED = "AUTO_REJECTED"


class FieldStatistics(BaseModel):
    """数据画像字段统计。"""

    physical_type: str | None = None
    row_count: int | None = None
    null_count: int | None = None
    null_rate: float | None = None
    distinct_count: int | None = None
    distinct_rate: float | None = None
    min: float | None = None
    max: float | None = None
    mean: float | None = None
    median: float | None = None
    quantiles: dict[str, float] | None = None
    datetime_min: str | None = None
    datetime_max: str | None = None
    time_span_days: float | None = None
    max_length: int | None = None
    avg_length: float | None = None
    top_values: list[dict[str, Any]] | None = None
    enum_candidates: list[str] | None = None
    is_identifier_candidate: bool = False
    duplicate_rate: float | None = None
    format_pattern: str | None = None


class TimeSeriesTimeAxis(BaseModel):
    timestamp_field: str
    timezone: str = "UTC"
    granularity: str | None = None
    granularity_seconds: float | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    sample_count: int = 0
    missing_interval_count: int = 0
    duplicate_timestamp_count: int = 0
    irregular_ratio: float = 0.0


class TimeSeriesEntity(BaseModel):
    entity_key: str
    fields: list[str] = Field(default_factory=list)
    values: dict[str, Any] = Field(default_factory=dict)


class TimeSeriesMetricProfile(BaseModel):
    count: int = 0
    missing_count: int = 0
    missing_rate: float = 0.0
    min: float | None = None
    max: float | None = None
    mean: float | None = None
    median: float | None = None
    std: float | None = None
    first_value: float | None = None
    last_value: float | None = None
    absolute_change: float | None = None
    relative_change: float | None = None
    quantiles: dict[str, float] = Field(default_factory=dict)


class TimeSeriesTrend(BaseModel):
    direction: Literal["UP", "DOWN", "FLAT"]
    slope: float = 0.0
    normalized_slope: float = 0.0
    strength: float = Field(default=0.0, ge=0.0, le=1.0)
    decision: TimeSeriesDecisionStatus = TimeSeriesDecisionStatus.AUTO_ACCEPTED


class TimeSeriesSeasonality(BaseModel):
    period_samples: int
    period_seconds: float | None = None
    period_label: str | None = None
    autocorrelation: float
    strength: float = Field(ge=0.0, le=1.0)
    decision: TimeSeriesDecisionStatus = TimeSeriesDecisionStatus.AUTO_ACCEPTED


class TimeSeriesChangePoint(BaseModel):
    timestamp: datetime
    score: float
    before_mean: float | None = None
    after_mean: float | None = None
    direction: Literal["UP", "DOWN"]
    decision: TimeSeriesDecisionStatus = TimeSeriesDecisionStatus.AUTO_ACCEPTED


class TimeSeriesSegment(BaseModel):
    state: Literal["LOW", "NORMAL", "HIGH"]
    start_time: datetime
    end_time: datetime
    duration_seconds: float
    mean: float
    max: float
    min: float
    decision: TimeSeriesDecisionStatus = TimeSeriesDecisionStatus.AUTO_ACCEPTED


class TimeSeriesEvent(BaseModel):
    event_type: Literal[
        "SPIKE",
        "DROP",
        "HIGH_LOAD",
        "LOW_LOAD",
        "CHANGE_UP",
        "CHANGE_DOWN",
    ]
    start_time: datetime
    end_time: datetime
    magnitude: float | None = None
    direction: Literal["UP", "DOWN", "FLAT"]
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    decision: TimeSeriesDecisionStatus = TimeSeriesDecisionStatus.AUTO_ACCEPTED
    evidence: list[str] = Field(default_factory=list)


class TimeSeriesAnomaly(BaseModel):
    start_time: datetime
    end_time: datetime
    score: float
    observed_value: float
    expected_value: float | None = None
    decision: TimeSeriesDecisionStatus = TimeSeriesDecisionStatus.AUTO_ACCEPTED
    evidence: list[str] = Field(default_factory=list)


class TimeSeriesRelationship(BaseModel):
    entity_key: str
    from_metric: str
    to_metric: str
    relation_type: Literal["CO_MOVING", "LEADS", "LAGS"]
    lag_samples: int = 0
    correlation: float
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    decision: TimeSeriesDecisionStatus = TimeSeriesDecisionStatus.AUTO_ACCEPTED


class StatisticalFeatureSet(BaseModel):
    count: int = 0
    valid_count: int = 0
    missing_count: int = 0
    missing_rate: float = 0.0
    mean: float | None = None
    median: float | None = None
    std: float | None = None
    variance: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    value_range: float | None = None
    q1: float | None = None
    q3: float | None = None
    iqr: float | None = None
    mad: float | None = None
    coefficient_of_variation: float | None = None
    skewness: float | None = None
    kurtosis: float | None = None
    zero_rate: float = 0.0
    unique_rate: float = 0.0
    first_value: float | None = None
    last_value: float | None = None
    absolute_change: float | None = None
    relative_change: float | None = None
    autocorrelation_lag1: float | None = None
    rolling_volatility_mean: float | None = None
    rolling_volatility_max: float | None = None


class ResourceMetricFeature(BaseModel):
    entity: TimeSeriesEntity
    metric_id: str
    feature_id: str
    statistics: StatisticalFeatureSet
    trend: TimeSeriesTrend | None = None
    seasonalities: list[TimeSeriesSeasonality] = Field(default_factory=list)
    change_points: list[TimeSeriesChangePoint] = Field(default_factory=list)
    segments: list[TimeSeriesSegment] = Field(default_factory=list)
    events: list[TimeSeriesEvent] = Field(default_factory=list)
    anomalies: list[TimeSeriesAnomaly] = Field(default_factory=list)


class ResourceQuality(BaseModel):
    row_count: int = 0
    entity_count: int = 0
    metric_count: int = 0
    completeness: float = 0.0
    duplicate_timestamp_count: int = 0
    irregular_ratio: float = 0.0
    issues: list[str] = Field(default_factory=list)


class ResourceChartPoint(BaseModel):
    timestamp: datetime
    value: float


class ResourceChartSeries(BaseModel):
    label: str
    entity_ref: str
    metric_id: str
    points: list[ResourceChartPoint] = Field(default_factory=list)


class StatisticalResourceDescriptor(BaseModel):
    schema_version: str = "resource_statistical_features_v1"
    resource_id: str
    resource_name: str
    source_file: str
    layout: Literal["long", "wide"]
    time_axis: TimeSeriesTimeAxis
    entity_fields: list[str] = Field(default_factory=list)
    value_fields: list[str] = Field(default_factory=list)
    quality: ResourceQuality
    features: list[ResourceMetricFeature] = Field(default_factory=list)
    charts: list[ResourceChartSeries] = Field(default_factory=list)
    correlations: list[TimeSeriesRelationship] = Field(default_factory=list)
    analyzer: str = "statistical_features_v1"
    generated_at: datetime


class AgentInputFile(BaseModel):
    file_id: str
    name: str
    role: Literal["timeseries", "schema", "structured_table"]
    format: str
    size_bytes: int = 0
    relative_path: str | None = None
    source_type: Literal["folder", "upload", "database"] = "upload"
    display_name: str | None = None
    table_name: str | None = None
    schema_name: str | None = None
    database_name: str | None = None
    database_type: str | None = None
    database_display_name: str | None = None
    time_field: str | None = None
    time_start: datetime | None = None
    time_end: datetime | None = None
    max_rows: int | None = None
    column_info: list[dict[str, Any]] = Field(default_factory=list)
    status: str = "UPLOADED"
    error: str | None = None


class AgentSchemaColumn(BaseModel):
    name: str
    data_type: str | None = None
    nullable: bool | None = None
    description: str | None = None
    is_primary_key: bool = False


class AgentSchemaTable(BaseModel):
    schema_name: str | None = None
    table_name: str
    columns: list[AgentSchemaColumn] = Field(default_factory=list)
    source_file: str


class AgentAttribute(BaseModel):
    entity_ref: str | None = None
    attribute_name: str
    value: Any
    description: str | None = None
    category: str | None = None
    business_type: str | None = None
    unit: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    decision: TimeSeriesDecisionStatus = TimeSeriesDecisionStatus.AUTO_ACCEPTED
    source_files: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    model_version: str | None = None


class AgentLLMResult(BaseModel):
    summary: str
    overall_status: str | None = None
    attributes: list[AgentAttribute] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    generated_at: datetime | None = None


class UnifiedResourceSource(BaseModel):
    source_type: Literal["folder", "upload", "database"]
    relative_path: str
    file_format: str
    file_size_bytes: int = 0
    database_type: str | None = None
    database_name: str | None = None
    schema_name: str | None = None
    table_name: str | None = None


class UnifiedResourceClassification(BaseModel):
    data_kind: Literal["time_series", "structured_table", "schema"]
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    reason: str


class ResourceColumnProfile(BaseModel):
    name: str
    data_type: str | None = None
    semantic_type: str | None = None
    nullable: bool | None = None
    is_primary_key: bool = False
    is_identifier_candidate: bool = False
    description: str | None = None


class ResourceSchemaProfile(BaseModel):
    row_count: int | None = None
    column_count: int | None = None
    columns: list[ResourceColumnProfile] = Field(default_factory=list)


class UnifiedResourceQuality(BaseModel):
    completeness: float | None = None
    null_value_count: int | None = None
    issue_count: int = 0
    issues: list[str] = Field(default_factory=list)


class ResourceProcessing(BaseModel):
    status: Literal["completed", "failed"] = "completed"
    parser_version: str = "unified_resource_v1"
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None


class UnifiedResourceReport(BaseModel):
    """文件和数据库表共用的统一解析结果。"""

    schema_version: str = "unified_resource_v1"
    report_id: str
    resource_id: str
    resource_type: Literal["file", "database_table"]
    resource_name: str
    display_name: str
    source: UnifiedResourceSource
    classification: UnifiedResourceClassification
    schema_profile: ResourceSchemaProfile
    time_axis: TimeSeriesTimeAxis | None = None
    entity_fields: list[str] = Field(default_factory=list)
    metric_fields: list[str] = Field(default_factory=list)
    quality: UnifiedResourceQuality
    statistics: dict[str, Any] = Field(default_factory=dict)
    attributes: list[AgentAttribute] = Field(default_factory=list)
    summary: str
    overall_status: str | None = None
    recommendations: list[str] = Field(default_factory=list)
    relationships: list[dict[str, Any]] = Field(default_factory=list)
    processing: ResourceProcessing
    generated_at: datetime


class AgentToolTrace(BaseModel):
    tool_name: str
    status: str
    summary: str


class AgentParseResult(BaseModel):
    schema_version: str = "agent_parse_result_v2"
    run_id: str
    status: AgentRunStatus
    input_files: list[AgentInputFile] = Field(default_factory=list)
    schema_tables: list[AgentSchemaTable] = Field(default_factory=list)
    reports: list[UnifiedResourceReport] = Field(default_factory=list)
    relationships: list[dict[str, Any]] = Field(default_factory=list)
    tool_trace: list[AgentToolTrace] = Field(default_factory=list)
    generated_at: datetime | None = None
    error: str | None = None


class AgentRunSummary(BaseModel):
    run_id: str
    status: AgentRunStatus
    current_step: str
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    input_file_count: int = 0
    schema_file_count: int = 0
    timeseries_file_count: int = 0
    report_count: int = 0
    result_ready: bool = False
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class AgentRunHistoryItem(BaseModel):
    run_id: str
    label: str
    status: AgentRunStatus
    current_step: str
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    input_file_count: int = 0
    report_count: int = 0
    created_at: datetime
    finished_at: datetime | None = None
    error: str | None = None


class ModelSettingsResponse(BaseModel):
    base_url: str
    model: str
    configured: bool = False
    key_masked: str | None = None
    source: Literal["runtime", "environment", "none"] = "none"
    expires_on: str | None = None


class ModelConnectionRequest(BaseModel):
    base_url: str = Field(min_length=1, max_length=1000)
    model: str = Field(min_length=1, max_length=255)
    api_key: str | None = Field(default=None, max_length=2048)


class ModelConnectionResponse(BaseModel):
    success: bool
    message: str


class ErrorResponse(BaseModel):
    detail: str


DatabaseType = Literal[
    "mysql",
    "mariadb",
    "postgresql",
    "sqlserver",
    "sqlite",
    "oracle",
]


class DatabaseConnectRequest(BaseModel):
    connection_name: str | None = Field(default=None, max_length=128)
    database_type: DatabaseType = "mysql"
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, max_length=128)
    password: SecretStr | None = Field(default=None, max_length=512)
    database: str = Field(min_length=1, max_length=1024)
    schema_name: str | None = Field(default=None, max_length=128)


class DatabaseColumnInfo(BaseModel):
    name: str
    data_type: str
    nullable: bool = True
    is_primary_key: bool = False
    description: str | None = None


class DatabaseTableInfo(BaseModel):
    schema_name: str
    table_name: str
    columns: list[DatabaseColumnInfo] = Field(default_factory=list)
    default_time_column: str | None = None


class DatabaseConnectionResponse(BaseModel):
    connection_id: str
    database_type: DatabaseType = "mysql"
    database_name: str
    display_name: str = ""
    tables: list[DatabaseTableInfo] = Field(default_factory=list)
    expires_at: datetime


class DatabaseTableSelection(BaseModel):
    schema_name: str | None = None
    table_name: str
    time_column: str | None = None
    time_start: datetime | None = None
    time_end: datetime | None = None


class DatabaseRunRequest(BaseModel):
    connection_id: str
    tables: list[DatabaseTableSelection] = Field(min_length=1, max_length=200)
    max_rows: int = Field(default=500, ge=1, le=500)
