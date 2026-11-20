"""统一 Agent 解析流程接口。"""

from __future__ import annotations

import csv
import io
import json
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
    AgentParseResult,
    AgentRunHistoryItem,
    AgentRunStatus,
    AgentRunSummary,
    DatabaseRunRequest,
    ErrorResponse,
    UnifiedResourceReport,
)
from ..services import profiler
from ..services.database_source import (
    DATABASE_LABELS,
    DatabaseSourceError,
    fetch_table_csv,
    get_connection_metadata,
)
from ..services.parsing_agent import ParsingAgent
from ..services.parsing_agent.storage import AgentRunNotFound, AgentRunStore
from ..storage import new_id, safe_filename

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])

SCHEMA_SUFFIXES = {".sql", ".ddl"}
STRUCTURED_SUFFIXES = {".json", ".csv", ".xlsx", ".xls"}
SCHEMA_HEADERS = {
    "table_name",
    "column_name",
    "data_type",
    "is_nullable",
    "description",
    "字段名",
    "字段类型",
    "表名",
}


def get_store() -> AgentRunStore:
    return AgentRunStore()


@router.get(
    "/runs",
    response_model=list[AgentRunHistoryItem],
    summary="获取最近运行记录",
)
def list_runs(limit: int = 20) -> list[AgentRunHistoryItem]:
    try:
        records = get_store().list_runs(limit=limit)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return [AgentRunHistoryItem.model_validate(item) for item in records]


@router.post(
    "/runs",
    response_model=AgentRunSummary,
    status_code=status.HTTP_202_ACCEPTED,
    summary="启动 Agent 主导的统一解析流程",
    responses={400: {"model": ErrorResponse}, 413: {"model": ErrorResponse}},
)
async def start_run(
    background_tasks: BackgroundTasks,
    files: Annotated[
        list[UploadFile] | None,
        File(description="时序数据和表结构文件，可混合上传"),
    ] = None,
    timeseries_files: Annotated[
        list[UploadFile] | None,
        File(description="兼容旧调用：显式指定时序文件"),
    ] = None,
    schema_files: Annotated[
        list[UploadFile] | None,
        File(description="兼容旧调用：显式指定表结构文件"),
    ] = None,
    relative_paths: Annotated[
        list[str] | None,
        Form(description="文件夹上传时的相对路径，顺序与 files 一致"),
    ] = None,
    sql_dialect: Annotated[str, Form(description="SQL DDL 方言")] = "mysql",
) -> AgentRunSummary:
    uploads: list[tuple[UploadFile, str | None]] = []
    uploads.extend((item, None) for item in (files or []))
    uploads.extend((item, "timeseries") for item in (timeseries_files or []))
    uploads.extend((item, "schema") for item in (schema_files or []))
    if not uploads:
        raise HTTPException(status_code=400, detail="至少需要上传一个文件。")

    settings = get_settings()
    if len(uploads) > settings.max_folder_file_count:
        raise HTTPException(
            status_code=413,
            detail=f"文件数量超过上限 {settings.max_folder_file_count}。",
        )
    infos: list[dict] = []
    payloads: list[tuple[dict, bytes]] = []
    seen: set[str] = set()
    total_bytes = 0
    for index, (upload, hint) in enumerate(uploads):
        raw_path = (
            relative_paths[index]
            if relative_paths and index < len(relative_paths) and relative_paths[index]
            else upload.filename
        )
        relative_path = _normalize_relative_path(raw_path or "")
        name = safe_filename(relative_path)
        if not name:
            raise HTTPException(status_code=400, detail="上传文件缺少文件名。")
        if relative_path in seen:
            raise HTTPException(status_code=400, detail=f"文件路径重复：{relative_path}")
        seen.add(relative_path)
        suffix = f".{name.rsplit('.', 1)[-1].lower()}" if "." in name else ""
        if suffix not in profiler.SUPPORTED_SUFFIXES and suffix not in SCHEMA_SUFFIXES | STRUCTURED_SUFFIXES:
            raise HTTPException(status_code=400, detail=f"文件格式不支持：{name}")
        content = await upload.read()
        if not content:
            raise HTTPException(status_code=400, detail=f"文件内容为空：{name}")
        total_bytes += len(content)
        if len(content) > settings.max_data_file_bytes:
            raise HTTPException(status_code=413, detail=f"文件超过大小上限：{name}")
        if total_bytes > settings.max_folder_total_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"文件夹总大小超过上限 {settings.max_folder_total_bytes} 字节。",
            )
        role = hint or classify_upload(name, content)
        if role == "schema" and len(content) > settings.max_schema_file_bytes:
            raise HTTPException(status_code=413, detail=f"表结构文件超过大小上限：{name}")
        info = {
            "file_id": new_id("file"),
            "name": name,
            "role": role,
            "format": suffix.lstrip("."),
            "size_bytes": len(content),
            "relative_path": relative_path,
            "display_name": relative_path,
            "source_type": "folder" if "/" in relative_path else "upload",
            "status": "UPLOADED",
            "error": None,
        }
        infos.append(info)
        payloads.append((info, content))

    if not infos:
        raise HTTPException(status_code=400, detail="没有可解析的文件。")

    store = get_store()
    record = store.create(
        timeseries_files=[item for item in infos if item["role"] == "timeseries"],
        schema_files=[item for item in infos if item["role"] == "schema"],
        structured_files=[item for item in infos if item["role"] == "structured_table"],
        input_files=infos,
    )
    run_id = record["run_id"]
    payload_by_id = {item["file_id"]: content for item, content in payloads}
    for info in infos:
        store.save_upload(
            run_id,
            info["file_id"],
            info["name"],
            payload_by_id[info["file_id"]],
        )
    store.update(run_id, sql_dialect=sql_dialect)
    background_tasks.add_task(ParsingAgent(store, settings).run, run_id)
    return _summary(store.load(run_id))


@router.post(
    "/runs/from-database",
    response_model=AgentRunSummary,
    status_code=status.HTTP_202_ACCEPTED,
    summary="从已连接的数据库表启动统一解析",
    responses={400: {"model": ErrorResponse}, 413: {"model": ErrorResponse}},
)
def start_database_run(
    request: DatabaseRunRequest,
    background_tasks: BackgroundTasks,
) -> AgentRunSummary:
    settings = get_settings()
    store = get_store()
    infos: list[dict] = []
    payloads: list[tuple[dict, bytes]] = []
    seen_tables: set[tuple[str, str]] = set()
    try:
        connection_meta = get_connection_metadata(request.connection_id)
    except DatabaseSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    database_type = connection_meta["database_type"]

    for selection in request.tables:
        schema_name = selection.schema_name or ""
        table_key = (schema_name, selection.table_name)
        if table_key in seen_tables:
            raise HTTPException(
                status_code=400,
                detail=f"数据表重复选择：{selection.table_name}",
            )
        seen_tables.add(table_key)
        try:
            content, table_info = fetch_table_csv(
                request.connection_id,
                selection,
                max_rows=request.max_rows,
                settings=settings,
            )
        except DatabaseSourceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        schema_name = table_info.schema_name
        time_field = selection.time_column or table_info.default_time_column
        relative_path = (
            f"{DATABASE_LABELS.get(database_type, database_type)}/"
            f"{table_info.schema_name}/{table_info.table_name}"
        )
        info = {
            "file_id": new_id("file"),
            "name": f"{table_info.table_name}.csv",
            "role": "timeseries" if time_field else "structured_table",
            "format": "csv",
            "size_bytes": len(content),
            "relative_path": relative_path,
            "display_name": relative_path,
            "source_type": "database",
            "database_type": database_type,
            "database_display_name": connection_meta["display_name"],
            "table_name": table_info.table_name,
            "schema_name": schema_name,
            "database_name": connection_meta["database_name"],
            "time_field": time_field,
            "time_start": (
                selection.time_start.isoformat() if selection.time_start else None
            ),
            "time_end": selection.time_end.isoformat() if selection.time_end else None,
            "max_rows": request.max_rows,
            "column_info": [
                column.model_dump(mode="json") for column in table_info.columns
            ],
            "status": "UPLOADED",
            "error": None,
        }
        infos.append(info)
        payloads.append((info, content))

    if not infos:
        raise HTTPException(status_code=400, detail="至少选择一张数据表。")

    record = store.create(
        timeseries_files=[item for item in infos if item["role"] == "timeseries"],
        schema_files=[],
        structured_files=[item for item in infos if item["role"] == "structured_table"],
        input_files=infos,
    )
    run_id = record["run_id"]
    payload_by_id = {item["file_id"]: content for item, content in payloads}
    for info in infos:
        store.save_upload(
            run_id,
            info["file_id"],
            info["name"],
            payload_by_id[info["file_id"]],
        )
    sql_dialects = {
        "mysql": "mysql",
        "mariadb": "mysql",
        "postgresql": "postgresql",
        "sqlserver": "tsql",
        "sqlite": "sqlite",
        "oracle": "oracle",
    }
    store.update(run_id, sql_dialect=sql_dialects.get(database_type, database_type))
    background_tasks.add_task(ParsingAgent(store, settings).run, run_id)
    return _summary(store.load(run_id))


@router.get(
    "/runs/{run_id}",
    response_model=AgentRunSummary,
    summary="查询 Agent 解析状态",
    responses={404: {"model": ErrorResponse}},
)
def get_run(run_id: str) -> AgentRunSummary:
    store = get_store()
    try:
        return _summary(store.load(run_id))
    except AgentRunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/runs/{run_id}/hide",
    summary="从最近运行记录中隐藏一次运行",
    responses={404: {"model": ErrorResponse}},
)
def hide_run(run_id: str) -> dict[str, bool]:
    try:
        get_store().hide_run(run_id)
    except AgentRunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"hidden": True}


@router.get(
    "/runs/{run_id}/result",
    response_model=AgentParseResult,
    summary="获取统一结构化解析结果",
    responses={404: {"model": ErrorResponse}},
)
def get_result(run_id: str) -> AgentParseResult:
    store = get_store()
    try:
        return AgentParseResult.model_validate(store.load_result(run_id))
    except AgentRunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/runs/{run_id}/resources/{resource_id}",
    response_model=UnifiedResourceReport,
    summary="获取单个文件或数据库表的统一解析 JSON",
    responses={404: {"model": ErrorResponse}},
)
def get_resource_result(run_id: str, resource_id: str) -> UnifiedResourceReport:
    store = get_store()
    try:
        return UnifiedResourceReport.model_validate(
            store.load_resource(run_id, resource_id)
        )
    except AgentRunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def classify_upload(name: str, content: bytes) -> str:
    """依据扩展名和内容自动判断角色。"""
    suffix = f".{name.rsplit('.', 1)[-1].lower()}" if "." in name else ""
    if suffix in SCHEMA_SUFFIXES:
        return "schema"
    if suffix == ".json":
        try:
            payload = json.loads(content.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return "timeseries"
        if isinstance(payload, dict) and (
            "tables" in payload
            or all(isinstance(value, (dict, list)) for value in payload.values())
        ):
            return "schema"
        return "timeseries"
    if suffix == ".csv":
        try:
            text = content.decode("utf-8-sig")
            header = next(csv.reader(io.StringIO(text), delimiter=","))
        except (UnicodeDecodeError, StopIteration):
            return "timeseries"
        normalized = {item.strip().lower() for item in header}
        if normalized & SCHEMA_HEADERS:
            return "schema"
    return "timeseries"


def _summary(record: dict) -> AgentRunSummary:
    input_files = record.get("input_files", [])
    timeseries = [item for item in input_files if item.get("role") == "timeseries"]
    schemas = [item for item in input_files if item.get("role") == "schema"]
    return AgentRunSummary(
        run_id=record["run_id"],
        status=AgentRunStatus(record["status"]),
        current_step=record.get("current_step", "unknown"),
        progress=record.get("progress", 0.0),
        input_file_count=len(input_files),
        schema_file_count=len(schemas),
        timeseries_file_count=len(timeseries),
        report_count=record.get("report_count", 0),
        result_ready=record.get("status") == AgentRunStatus.COMPLETED.value,
        created_at=record["created_at"],
        started_at=record.get("started_at"),
        finished_at=record.get("finished_at"),
        error=record.get("error"),
    )


def _normalize_relative_path(raw: str) -> str:
    value = raw.replace("\\", "/").strip().lstrip("/")
    parts = [part for part in value.split("/") if part not in {"", "."}]
    if not parts or ".." in parts:
        raise HTTPException(status_code=400, detail="上传文件路径无效。")
    return "/".join(parts)
