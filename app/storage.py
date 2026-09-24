"""批次元数据与解析结果的落盘存储。

目录结构：

    data/batches/{batch_id}/
        batch.json            # 批次元数据（状态、配对、描述）
        uploads/              # 上传的 SQL 与数据原件
        results/{code}.json   # 每个资源一份固定 JSON
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import get_settings
from .schemas import BatchStatus, ResourceProfile, SqlSummary

# 同一批次的读改写不并发（解析在后台线程中串行推进）。
_LOCK = threading.RLock()

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._一-鿿-]")


def new_id(prefix: str) -> str:
    """生成带前缀的短 ID，如 res_7f3a9c1d2b4e。"""
    return f"{prefix}_{os.urandom(6).hex()}"


def now() -> datetime:
    return datetime.now(timezone.utc)


def safe_filename(raw: str) -> str:
    """只保留基名并剔除危险字符，防止路径穿越写到批次目录之外。"""
    base = Path(raw.replace("\\", "/")).name.strip()
    base = base.lstrip(".") or "unnamed"
    cleaned = _UNSAFE_NAME.sub("_", base)
    return cleaned[:180] or "unnamed"


class BatchNotFound(FileNotFoundError):
    pass


class ResourceNotFound(FileNotFoundError):
    pass


class AgentRunNotFound(FileNotFoundError):
    pass


# ---------------------------------------------------------------- 内部记录


class BatchStore:
    """批次存储。所有写入先落临时文件再替换，避免进程中断留下半个 JSON。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else get_settings().data_dir

    # ---- 路径 ----

    def batch_dir(self, batch_id: str) -> Path:
        return self.root / safe_filename(batch_id)

    def uploads_dir(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / "uploads"

    def results_dir(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / "results"

    def _meta_path(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / "batch.json"

    def _agent_path(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / "agent.json"

    # ---- 读写 ----

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _read_json(self, path: Path) -> Any:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    # ---- 批次 ----

    def create(self, sql_dialect: str) -> dict[str, Any]:
        batch_id = new_id("bat")
        record = {
            "batch_id": batch_id,
            "status": BatchStatus.UPLOADED.value,
            "sql_dialect": sql_dialect,
            "created_at": now().isoformat(),
            "started_at": None,
            "finished_at": None,
            "resources": [],
            "unmatched_data_files": [],
            "error": None,
            "resource_descriptions": {},
            "field_descriptions": {},
            "provider": None,
        }
        with _LOCK:
            self._write_json(self._meta_path(batch_id), record)
        return record

    def load(self, batch_id: str) -> dict[str, Any]:
        path = self._meta_path(batch_id)
        if not path.exists():
            raise BatchNotFound(f"批次不存在: {batch_id}")
        with _LOCK:
            return self._read_json(path)

    def save(self, record: dict[str, Any]) -> None:
        with _LOCK:
            self._write_json(self._meta_path(record["batch_id"]), record)

    def update(self, batch_id: str, **changes: Any) -> dict[str, Any]:
        """读出-改-写。调用方持有的 record 会过期，需用返回值。"""
        with _LOCK:
            record = self.load(batch_id)
            record.update(changes)
            self.save(record)
            return record

    def save_upload(self, batch_id: str, filename: str, content: bytes) -> Path:
        target = self.uploads_dir(batch_id) / safe_filename(filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            target.write_bytes(content)
        return target

    # ---- 资源 ----

    def add_resources(self, batch_id: str, resources: list[dict[str, Any]]) -> dict[str, Any]:
        record = self.load(batch_id)
        # 同名 SQL 文件在同一批次内只保留一份，避免 resource_code 冲突。
        seen = {r["resource_code"] for r in record["resources"]}
        for res in resources:
            if res["resource_code"] not in seen:
                record["resources"].append(res)
                seen.add(res["resource_code"])
        self.save(record)
        return record

    def find_resource(self, record: dict[str, Any], resource_code: str) -> dict[str, Any]:
        for res in record["resources"]:
            if res["resource_code"] == resource_code:
                return res
        raise ResourceNotFound(f"资源不存在: {resource_code}")

    def update_resource(self, batch_id: str, resource_code: str, **changes: Any) -> dict[str, Any]:
        with _LOCK:
            record = self.load(batch_id)
            res = self.find_resource(record, resource_code)
            res.update(changes)
            self.save(record)
            return record

    # ---- 解析结果 ----

    def result_path(self, batch_id: str, resource_code: str) -> Path:
        return self.results_dir(batch_id) / f"{safe_filename(resource_code)}.json"

    def save_result(self, batch_id: str, resource_code: str, payload: dict[str, Any]) -> None:
        self._write_json(self.result_path(batch_id, resource_code), payload)

    def load_result(self, batch_id: str, resource_code: str) -> dict[str, Any]:
        path = self.result_path(batch_id, resource_code)
        if not path.exists():
            raise ResourceNotFound(f"资源结果不存在: {resource_code}")
        return self._read_json(path)

    def load_all_results(self, batch_id: str) -> list[dict[str, Any]]:
        record = self.load(batch_id)
        results = []
        for res in record["resources"]:
            try:
                results.append(self.load_result(batch_id, res["resource_code"]))
            except ResourceNotFound:
                continue  # 失败或尚未完成的资源不产出结果文件
        return results

    # ---- 增强型 Agent ----

    def create_agent_run(self, batch_id: str, *, require_api: bool) -> dict[str, Any]:
        self.load(batch_id)  # 批次不存在时直接失败
        payload = {
            "agent_run_id": new_id("agt"),
            "batch_id": batch_id,
            "status": "QUEUED",
            "provider": None,
            "model": None,
            "require_api": require_api,
            "current_step": "queued",
            "progress": 0.0,
            "started_at": None,
            "finished_at": None,
            "events": [],
            "review_items": [],
            "error": None,
        }
        with _LOCK:
            self._write_json(self._agent_path(batch_id), payload)
        return payload

    def load_agent_run(self, batch_id: str) -> dict[str, Any]:
        path = self._agent_path(batch_id)
        if not path.exists():
            raise AgentRunNotFound(f"增强型 Agent 运行不存在: {batch_id}")
        with _LOCK:
            return self._read_json(path)

    def save_agent_run(self, payload: dict[str, Any]) -> None:
        with _LOCK:
            self._write_json(self._agent_path(payload["batch_id"]), payload)

    def update_agent_run(self, batch_id: str, **changes: Any) -> dict[str, Any]:
        with _LOCK:
            payload = self.load_agent_run(batch_id)
            payload.update(changes)
            self.save_agent_run(payload)
            return payload

    def append_agent_event(self, batch_id: str, event: dict[str, Any]) -> dict[str, Any]:
        with _LOCK:
            payload = self.load_agent_run(batch_id)
            payload.setdefault("events", []).append(event)
            self.save_agent_run(payload)
            return payload


def build_resource_descriptor(
    *,
    resource_code: str,
    sql_file: str,
    name: str | None,
    description: str | None,
    source_tables: list[str],
    sql_summary: SqlSummary,
    profile: ResourceProfile,
    tags: list[str],
    fields: list[dict[str, Any]],
    provider_name: str | None,
) -> dict[str, Any]:
    """组装方案 §5 的固定 JSON。保持字段顺序稳定，便于结果比对。"""
    return {
        "resource_id": new_id("res"),
        "resource_code": resource_code,
        "name": name,
        "description": description,
        "source_tables": source_tables,
        "sql_summary": sql_summary.model_dump(by_alias=True),
        "profile": profile.model_dump(),
        "tags": tags,
        "fields": fields,
        "sql_file": sql_file,
        "generated_at": now().isoformat(),
        "semantic_provider": provider_name,
    }
