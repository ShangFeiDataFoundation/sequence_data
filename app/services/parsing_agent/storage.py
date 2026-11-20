"""统一 Agent 运行文件存储。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from ...config import get_settings
from ...storage import new_id, now, safe_filename

_LOCK = threading.RLock()


class AgentRunNotFound(FileNotFoundError):
    pass


class AgentRunStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else get_settings().agent_runs_dir

    def run_dir(self, run_id: str) -> Path:
        return self.root / safe_filename(run_id)

    def uploads_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "uploads"

    def run_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "run.json"

    def result_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "result.json"

    def resources_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "resources"

    def resource_path(self, run_id: str, resource_id: str) -> Path:
        return self.resources_dir(run_id) / f"{safe_filename(resource_id)}.json"

    def hidden_path(self) -> Path:
        return self.root / "hidden-runs.json"

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def create(
        self,
        *,
        timeseries_files: list[dict[str, Any]],
        schema_files: list[dict[str, Any]],
        structured_files: list[dict[str, Any]] | None = None,
        input_files: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        run_id = new_id("run")
        structured_files = structured_files or []
        all_files = input_files or [*timeseries_files, *schema_files, *structured_files]
        payload = {
            "run_id": run_id,
            "status": "QUEUED",
            "current_step": "uploaded",
            "progress": 0.0,
            "created_at": now().isoformat(),
            "started_at": None,
            "finished_at": None,
            "timeseries_files": timeseries_files,
            "schema_files": schema_files,
            "structured_files": structured_files,
            "input_files": all_files,
            "tool_trace": [],
            "error": None,
        }
        with _LOCK:
            self._write_json(self.run_path(run_id), payload)
        return payload

    def load(self, run_id: str) -> dict[str, Any]:
        path = self.run_path(run_id)
        if not path.exists():
            raise AgentRunNotFound(f"Agent 运行不存在: {run_id}")
        with _LOCK, path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def update(self, run_id: str, **changes: Any) -> dict[str, Any]:
        with _LOCK:
            payload = self.load(run_id)
            payload.update(changes)
            self._write_json(self.run_path(run_id), payload)
            return payload

    def save_upload(self, run_id: str, file_id: str, name: str, content: bytes) -> Path:
        target = self.uploads_dir(run_id) / f"{safe_filename(file_id)}__{safe_filename(name)}"
        target.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            target.write_bytes(content)
        return target

    def upload_path(self, run_id: str, file_id: str, name: str) -> Path:
        return self.uploads_dir(run_id) / f"{safe_filename(file_id)}__{safe_filename(name)}"

    def save_result(self, run_id: str, payload: dict[str, Any]) -> None:
        with _LOCK:
            self._write_json(self.result_path(run_id), payload)

    def load_result(self, run_id: str) -> dict[str, Any]:
        path = self.result_path(run_id)
        if not path.exists():
            raise AgentRunNotFound(f"Agent 结果尚未生成: {run_id}")
        with _LOCK, path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def save_resource(
        self,
        run_id: str,
        resource_id: str,
        payload: dict[str, Any],
    ) -> Path:
        path = self.resource_path(run_id, resource_id)
        with _LOCK:
            self._write_json(path, payload)
        return path

    def load_resource(self, run_id: str, resource_id: str) -> dict[str, Any]:
        path = self.resource_path(run_id, resource_id)
        if not path.exists():
            raise AgentRunNotFound(
                f"资源结果不存在: {run_id}/{resource_id}"
            )
        with _LOCK, path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def list_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        with _LOCK:
            hidden = self._hidden_run_ids()
            records: list[dict[str, Any]] = []
            for path in self.root.glob("*/run.json"):
                try:
                    with path.open("r", encoding="utf-8") as handle:
                        payload = json.load(handle)
                except (OSError, ValueError, TypeError):
                    continue
                run_id = str(payload.get("run_id") or path.parent.name)
                if run_id in hidden:
                    continue
                input_files = payload.get("input_files") or []
                first_name = (
                    input_files[0].get("relative_path")
                    or input_files[0].get("name")
                    if input_files
                    else run_id
                )
                label = (
                    f"{first_name} 等 {len(input_files)} 个资源"
                    if len(input_files) > 1
                    else str(first_name)
                )
                records.append(
                    {
                        "run_id": run_id,
                        "label": label,
                        "status": payload.get("status", "FAILED"),
                        "current_step": payload.get("current_step", "unknown"),
                        "progress": payload.get("progress", 0.0),
                        "input_file_count": len(input_files),
                        "report_count": int(payload.get("report_count") or 0),
                        "created_at": payload.get("created_at"),
                        "finished_at": payload.get("finished_at"),
                        "error": payload.get("error"),
                    }
                )
            records.sort(
                key=lambda item: str(item.get("created_at") or ""),
                reverse=True,
            )
            return records[: max(1, min(limit, 100))]

    def hide_run(self, run_id: str) -> None:
        self.load(run_id)
        with _LOCK:
            hidden = self._hidden_run_ids()
            hidden.add(run_id)
            self._write_json(
                self.hidden_path(),
                {"run_ids": sorted(hidden)},
            )

    def _hidden_run_ids(self) -> set[str]:
        path = self.hidden_path()
        if not path.exists():
            return set()
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError, TypeError):
            return set()
        values = payload.get("run_ids", []) if isinstance(payload, dict) else []
        return {str(value) for value in values if value}
