"""批次相关接口（方案 §6）。"""

from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)

from ..config import get_settings
from ..schemas import (
    AgentEvent,
    AgentEventsResponse,
    AgentReviewItem,
    AgentRunRequest,
    AgentRunResponse,
    BatchCreatedResponse,
    BatchResultsResponse,
    BatchStatus,
    BatchStatusResponse,
    ErrorResponse,
    ResourceDescriptor,
    ResourceStatus,
    ResourceStatusItem,
    StartParsingRequest,
)
from ..services import profiler
from ..services.agent import run_enhanced_agent
from ..services.pairing import is_sql_file
from ..services.pipeline import build_previews, run_batch
from ..services.sql_parser import SUPPORTED_DIALECTS, is_supported_dialect
from ..storage import (
    AgentRunNotFound,
    BatchNotFound,
    BatchStore,
    ResourceNotFound,
    safe_filename,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/batches", tags=["batches"])


def get_store() -> BatchStore:
    return BatchStore()


# ---------------------------------------------------------------- 上传


@router.post(
    "",
    response_model=BatchCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="上传 SQL 与数据文件，自动配对并返回字段预览",
    responses={400: {"model": ErrorResponse}},
)
async def create_batch(
    sql_dialect: Annotated[str, Form(description="SQL 方言，如 mysql、postgresql、hive")],
    sql_files: Annotated[list[UploadFile], File(description="一个或多个 .sql 文件")],
    data_files: Annotated[list[UploadFile] | None, File(description="CSV、Parquet、XLSX 或 JSON Lines 文件")] = None,
    mappings: Annotated[str | None, Form(description="手动配对映射的 JSON 字符串")] = None,
) -> BatchCreatedResponse:
    if not is_supported_dialect(sql_dialect):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的 SQL 方言 {sql_dialect!r}，可选：{', '.join(sorted(SUPPORTED_DIALECTS))}",
        )
    if not sql_files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="至少需要上传一个 .sql 文件。")

    manual_mappings = _parse_mappings(mappings)
    settings = get_settings()
    store = get_store()

    # 先把所有上传读入内存并完成校验，避免校验失败时留下半个批次。
    sql_payloads = await _collect_uploads(sql_files, is_sql=True, max_bytes=settings.max_sql_file_bytes)
    data_payloads = await _collect_uploads(
        data_files or [], is_sql=False, max_bytes=settings.max_data_file_bytes
    )
    if not data_payloads:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="至少需要上传一个数据文件（CSV、Parquet、XLSX 或 JSON Lines）。",
        )

    # 建批次并落盘
    record = store.create(sql_dialect)
    batch_id = record["batch_id"]
    for name, content in sql_payloads + data_payloads:
        store.save_upload(batch_id, name, content)

    sql_names = [name for name, _ in sql_payloads]
    data_names = [name for name, _ in data_payloads]

    previews, pairing = build_previews(
        store, batch_id, sql_names, data_names, sql_dialect, manual_mappings
    )
    store.add_resources(
        batch_id,
        [
            {
                "resource_code": p.resource_code,
                "sql_file": p.sql_file,
                "data_file": p.data_file,
                "matched_by": p.matched_by,
                "status": ResourceStatus.PENDING.value,
                "progress": 0.0,
                "field_count": 0,
                "error": None,
                "output_fields": p.output_fields,
                "parse_error": p.parse_error,
            }
            for p in previews
        ],
    )
    store.update(
        batch_id,
        status=BatchStatus.PREVIEW_READY.value,
        unmatched_data_files=pairing.unmatched_data_files,
    )

    return BatchCreatedResponse(
        batch_id=batch_id,
        status=BatchStatus.PREVIEW_READY,
        sql_dialect=sql_dialect,
        resources=previews,
        unmatched_data_files=pairing.unmatched_data_files,
        unmatched_sql_files=[p.sql_file for p in previews if not p.data_file],
    )


# ---------------------------------------------------------------- 启动解析


@router.post(
    "/{batch_id}/start",
    response_model=BatchStatusResponse,
    summary="提交 SQL/字段描述并启动完整解析",
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def start_parsing(
    batch_id: str,
    request: StartParsingRequest,
    background_tasks: BackgroundTasks,
) -> BatchStatusResponse:
    store = get_store()
    _prepare_parsing(store, batch_id, request)
    background_tasks.add_task(run_batch, batch_id, store)
    return _build_status_response(store.load(batch_id))


# ---------------------------------------------------------------- 增强型 Agent


@router.post(
    "/{batch_id}/agent-runs",
    response_model=AgentRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="启动云端模型驱动的增强型 Agent",
    responses={
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
)
def start_agent_run(
    batch_id: str,
    request: AgentRunRequest,
    background_tasks: BackgroundTasks,
) -> AgentRunResponse:
    store = get_store()
    _prepare_parsing(store, batch_id, request)

    settings = get_settings()
    require_api = (
        settings.agent_require_api
        if request.require_api is None
        else request.require_api
    )
    run = store.create_agent_run(batch_id, require_api=require_api)
    background_tasks.add_task(run_enhanced_agent, batch_id, store)
    return _build_agent_response(run)


@router.get(
    "/{batch_id}/agent-runs/latest",
    response_model=AgentRunResponse,
    summary="查询最近一次增强型 Agent 状态",
    responses={404: {"model": ErrorResponse}},
)
def get_latest_agent_run(batch_id: str) -> AgentRunResponse:
    store = get_store()
    _load_or_404(store, batch_id)
    return _build_agent_response(_load_agent_or_404(store, batch_id))


@router.get(
    "/{batch_id}/agent-runs/latest/events",
    response_model=AgentEventsResponse,
    summary="查询最近一次增强型 Agent 执行事件",
    responses={404: {"model": ErrorResponse}},
)
def get_latest_agent_events(batch_id: str) -> AgentEventsResponse:
    store = get_store()
    _load_or_404(store, batch_id)
    run = _load_agent_or_404(store, batch_id)
    return AgentEventsResponse(
        agent_run_id=run["agent_run_id"],
        events=[AgentEvent.model_validate(event) for event in run.get("events", [])],
    )


# ---------------------------------------------------------------- 查询


@router.get(
    "/{batch_id}",
    response_model=BatchStatusResponse,
    summary="查询解析进度和每份 SQL 的解析状态",
    responses={404: {"model": ErrorResponse}},
)
def get_batch(batch_id: str) -> BatchStatusResponse:
    store = get_store()
    return _build_status_response(_load_or_404(store, batch_id))


@router.get(
    "/{batch_id}/results",
    response_model=BatchResultsResponse,
    summary="获取该任务全部 SQL 的固定 JSON 解析结果",
    responses={404: {"model": ErrorResponse}},
)
def get_all_results(batch_id: str) -> BatchResultsResponse:
    store = get_store()
    record = _load_or_404(store, batch_id)
    results = store.load_all_results(batch_id)
    return BatchResultsResponse(
        batch_id=batch_id,
        status=BatchStatus(record["status"]),
        results=[ResourceDescriptor.model_validate(r) for r in results],
    )


@router.get(
    "/{batch_id}/results/{resource_code}",
    response_model=ResourceDescriptor,
    summary="获取指定 SQL 对应的一份固定 JSON 解析结果",
    responses={404: {"model": ErrorResponse}},
)
def get_one_result(batch_id: str, resource_code: str) -> ResourceDescriptor:
    store = get_store()
    _load_or_404(store, batch_id)
    try:
        payload = store.load_result(batch_id, resource_code)
    except ResourceNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return ResourceDescriptor.model_validate(payload)


# ---------------------------------------------------------------- 内部


def _prepare_parsing(
    store: BatchStore,
    batch_id: str,
    request: StartParsingRequest,
) -> dict:
    """校验并准备一次经典或增强型解析运行。"""
    record = _load_or_404(store, batch_id)

    if record["status"] == BatchStatus.PARSING.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该批次正在解析中。")
    if not record["resources"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该批次没有可解析的资源。")

    changes: dict = {
        "resource_descriptions": request.resource_descriptions or {},
        "field_descriptions": request.field_descriptions or {},
        "status": BatchStatus.PARSING.value,
        "started_at": None,
        "finished_at": None,
        "error": None,
    }

    if request.mappings:
        changes["resources"] = _apply_mappings(store, batch_id, record, request.mappings)
    else:
        changes["resources"] = [
            {**res, "status": ResourceStatus.PENDING.value, "progress": 0.0, "error": None}
            for res in record["resources"]
        ]

    return store.update(batch_id, **changes)


async def _collect_uploads(
    uploads: list[UploadFile], *, is_sql: bool, max_bytes: int
) -> list[tuple[str, bytes]]:
    """校验并读入上传文件，返回 (安全文件名, 内容) 列表。"""
    collected: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for upload in uploads:
        name = _validate_upload_name(upload.filename, is_sql=is_sql)
        if name in seen:
            kind = "SQL" if is_sql else "数据"
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=f"{kind}文件名重复：{name}"
            )
        seen.add(name)
        collected.append((name, await _read_limited(upload, max_bytes)))
    return collected


def _apply_mappings(
    store: BatchStore, batch_id: str, record: dict, mappings: dict[str, str]
) -> list[dict]:
    """按启动请求修正配对关系。值为空串表示解除配对。"""
    resources = []
    seen_codes: set[str] = set()
    for res in record["resources"]:
        code = res["resource_code"]
        new_data = mappings.get(res["sql_file"], mappings.get(code, res.get("data_file")))
        if new_data == "":
            new_data = None
        if new_data and not (store.uploads_dir(batch_id) / safe_filename(new_data)).exists():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"映射的数据文件不存在：{new_data}",
            )
        resources.append(
            {
                **res,
                "data_file": new_data,
                "matched_by": "manual" if new_data != res.get("data_file") else res.get("matched_by", "auto"),
                "status": ResourceStatus.PENDING.value,
                "progress": 0.0,
                "error": None,
            }
        )
        seen_codes.add(code)
    return resources


def _build_status_response(record: dict) -> BatchStatusResponse:
    resources = [
        ResourceStatusItem(
        resource_code=res["resource_code"],
        sql_file=res["sql_file"],
        data_file=res.get("data_file"),
        matched_by=res.get("matched_by", "none"),
        status=ResourceStatus(res["status"]),
        progress=res.get("progress", 0.0),
        field_count=res.get("field_count", 0),
        output_fields=res.get("output_fields", []),
        parse_error=res.get("parse_error"),
        error=res.get("error"),
    )
        for res in record["resources"]
    ]
    return BatchStatusResponse(
        batch_id=record["batch_id"],
        status=BatchStatus(record["status"]),
        sql_dialect=record["sql_dialect"],
        created_at=record["created_at"],
        started_at=record.get("started_at"),
        finished_at=record.get("finished_at"),
        total_resources=len(resources),
        completed_resources=sum(1 for r in resources if r.status == ResourceStatus.COMPLETED),
        failed_resources=sum(1 for r in resources if r.status == ResourceStatus.FAILED),
        resources=resources,
        error=record.get("error"),
    )


def _build_agent_response(payload: dict) -> AgentRunResponse:
    return AgentRunResponse(
        agent_run_id=payload["agent_run_id"],
        batch_id=payload["batch_id"],
        status=payload["status"],
        provider=payload.get("provider"),
        model=payload.get("model"),
        require_api=payload.get("require_api", False),
        current_step=payload.get("current_step", "unknown"),
        progress=payload.get("progress", 0.0),
        started_at=payload.get("started_at"),
        finished_at=payload.get("finished_at"),
        event_count=len(payload.get("events", [])),
        review_items=[
            AgentReviewItem.model_validate(item)
            for item in payload.get("review_items", [])
        ],
        error=payload.get("error"),
    )


def _load_or_404(store: BatchStore, batch_id: str) -> dict:
    try:
        return store.load(batch_id)
    except BatchNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


def _load_agent_or_404(store: BatchStore, batch_id: str) -> dict:
    try:
        return store.load_agent_run(batch_id)
    except AgentRunNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


def _validate_upload_name(filename: str | None, *, is_sql: bool) -> str:
    if not filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="上传文件缺少文件名。")
    name = safe_filename(filename)
    if is_sql:
        if not is_sql_file(name):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=f"不是 .sql 文件：{name}"
            )
    elif not profiler.is_supported(name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的数据文件格式：{name}（支持 CSV、Parquet、XLSX、JSON Lines）",
        )
    return name


async def _read_limited(upload: UploadFile, max_bytes: int) -> bytes:
    content = await upload.read()
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"文件 {upload.filename} 超过大小上限 {max_bytes // (1024 * 1024)} MB。",
        )
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"文件 {upload.filename} 内容为空。"
        )
    return content


def _parse_mappings(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"mappings 不是合法 JSON：{exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="mappings 必须是对象。")
    return {str(k): str(v) for k, v in parsed.items()}
