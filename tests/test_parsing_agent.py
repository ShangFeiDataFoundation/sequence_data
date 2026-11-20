"""统一 Agent 解析流程测试。"""

from __future__ import annotations

import json
import sys
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.schemas import (
    AgentAttribute,
    AgentLLMResult,
    DatabaseColumnInfo,
    DatabaseConnectionResponse,
    DatabaseTableInfo,
)
from app.services.parsing_agent.llm import (
    LLMResponseError,
    OpenAICompatibleLLMClient,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SCHEMA = PROJECT_ROOT / "samples" / "comac_demo" / "c919_sensor_schema.sql"
ENGINE = PROJECT_ROOT / "samples" / "comac_demo" / "engine_readings.csv"
HYDRAULIC = PROJECT_ROOT / "samples" / "comac_demo" / "hydraulic_readings.csv"
SCHEMA_CSV = PROJECT_ROOT / "samples" / "comac_demo" / "hydraulic_schema_columns.csv"
SCHEMA_JSON = PROJECT_ROOT / "samples" / "comac_demo" / "flight_phase_schema.json"
API = "/api/v1/agent"
LLM_CALLS: list[str] = []


class _StubLLMClient:
    provider = "stub"
    model = "stub-llm"

    def analyze(self, payload):
        LLM_CALLS.append(payload["resource"]["resource_name"])
        return AgentLLMResult(
            summary="综合统计特征和表结构后生成的最终结果。",
            overall_status="WATCH",
            provider=self.provider,
            model=self.model,
            attributes=[
                AgentAttribute(
                    entity_ref="C919-SYN-001.engine_left",
                    attribute_name="engine_health",
                    value="排气温度参数存在轻微偏离，建议继续观察。",
                    description="结合趋势、分位数和异常特征生成文本型结论。",
                    category="health_assessment",
                    confidence=0.86,
                    source_files=["engine_readings.csv"],
                    source_refs=["C919-SYN-001.engine_left.egt_c"],
                    evidence=["rolling volatility is above baseline"],
                ),
                AgentAttribute(
                    attribute_name="fleet_overview",
                    value={
                        "normal_parameters": 18,
                        "watch_parameters": 9,
                    },
                    description="机队整体参数状态汇总。",
                    category="summary",
                    confidence=0.9,
                ),
            ],
            recommendations=["检查发动机参数趋势"],
        )

    def close(self):
        return None


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("AGENT_RUNS_DIR", str(tmp_path / "agent_runs"))
    monkeypatch.setenv("LLM_ENABLE_API", "false")
    monkeypatch.setenv("LLM_REQUIRED", "false")
    from app.config import get_settings
    from app.main import app

    get_settings.cache_clear()
    return TestClient(app)


def test_agent_pipeline_combines_schema_and_time_series(tmp_path, monkeypatch):
    LLM_CALLS.clear()
    monkeypatch.setattr(
        "app.services.parsing_agent.agent.build_llm_client",
        lambda settings: _StubLLMClient(),
    )
    paths = [SCHEMA, ENGINE, HYDRAULIC, SCHEMA_CSV, SCHEMA_JSON]
    with _client(tmp_path, monkeypatch) as client, ExitStack() as stack:
        uploads = []
        for path in paths:
            handle = stack.enter_context(path.open("rb"))
            uploads.append(("files", (path.name, handle, "application/octet-stream")))
        started = client.post(
            f"{API}/runs",
            data={"sql_dialect": "mysql"},
            files=uploads,
        )
        assert started.status_code == 202, started.text
        run_id = started.json()["run_id"]
        run = client.get(f"{API}/runs/{run_id}").json()
        assert run["status"] == "COMPLETED"
        assert run["result_ready"] is True

        result = client.get(f"{API}/runs/{run_id}/result")
    assert result.status_code == 200, result.text
    payload = result.json()
    assert payload["schema_version"] == "agent_parse_result_v2"
    assert len(payload["schema_tables"]) == 4
    assert {item["source_file"] for item in payload["schema_tables"]} <= {
        SCHEMA.name,
        SCHEMA_CSV.name,
        SCHEMA_JSON.name,
    }
    assert "resources" not in payload
    assert len(payload["reports"]) == 5
    assert "llm_result" not in payload
    assert len(LLM_CALLS) == 5
    assert all(len(report["attributes"]) == 2 for report in payload["reports"])
    assert all(report["summary"] for report in payload["reports"])
    assert any(
        item["tool_name"] == "parse_resource"
        for item in payload["tool_trace"]
    )
    assert any(
        item["tool_name"] == "invoke_resource_llm_analysis"
        for item in payload["tool_trace"]
    )
    time_reports = [
        item
        for item in payload["reports"]
        if item["classification"]["data_kind"] == "time_series"
    ]
    schema_reports = [
        item
        for item in payload["reports"]
        if item["classification"]["data_kind"] == "schema"
    ]
    assert len(time_reports) == 2
    assert len(schema_reports) == 3
    assert all("chart_series" not in item for item in payload["reports"])
    assert all(
        item["statistics"]["metric_summaries"] for item in time_reports
    )
    assert all(item["statistics"]["chart_series"] for item in time_reports)
    assert all(
        "metric_summaries" not in item["statistics"] for item in schema_reports
    )
    assert {item["schema_version"] for item in payload["reports"]} == {
        "unified_resource_v1"
    }
    assert all(item["source"]["relative_path"] for item in payload["reports"])

    resource_id = payload["reports"][0]["resource_id"]
    with _client(tmp_path, monkeypatch) as client:
        resource_result = client.get(f"{API}/runs/{run_id}/resources/{resource_id}")
    assert resource_result.status_code == 200
    assert resource_result.json()["schema_version"] == "unified_resource_v1"


def test_data_only_folder_builds_one_json_per_file(tmp_path, monkeypatch):
    LLM_CALLS.clear()
    monkeypatch.setattr(
        "app.services.parsing_agent.agent.build_llm_client",
        lambda settings: _StubLLMClient(),
    )
    with _client(tmp_path, monkeypatch) as client, ExitStack() as stack:
        uploads = []
        for path in (ENGINE, HYDRAULIC):
            handle = stack.enter_context(path.open("rb"))
            uploads.append(("files", (path.name, handle, "text/csv")))
        started = client.post(
            f"{API}/runs",
            data={
                "relative_paths": [
                    "comac_demo/readings/engine_readings.csv",
                    "comac_demo/readings/hydraulic_readings.csv",
                ]
            },
            files=uploads,
        )
        assert started.status_code == 202, started.text
        run_id = started.json()["run_id"]
        run = client.get(f"{API}/runs/{run_id}").json()
        result = client.get(f"{API}/runs/{run_id}/result").json()

    assert run["status"] == "COMPLETED"
    assert run["report_count"] == 2
    assert len(result["reports"]) == 2
    assert len(LLM_CALLS) == 2
    assert {
        item["source"]["relative_path"] for item in result["reports"]
    } == {
        "comac_demo/readings/engine_readings.csv",
        "comac_demo/readings/hydraulic_readings.csv",
    }


def test_database_run_builds_one_json_per_selected_table(tmp_path, monkeypatch):
    engine_bytes = ENGINE.read_bytes()
    schema_csv_bytes = SCHEMA_CSV.read_bytes()

    def fake_fetch(connection_id, selection, *, max_rows, settings):
        assert connection_id == "dbc_test"
        assert max_rows == 500
        if selection.table_name == "engine_readings":
            return engine_bytes, DatabaseTableInfo(
                schema_name="aircraft_monitor",
                table_name="engine_readings",
                columns=[
                    DatabaseColumnInfo(name="timestamp", data_type="datetime"),
                    DatabaseColumnInfo(name="aircraft_id", data_type="varchar"),
                    DatabaseColumnInfo(name="sensor_id", data_type="varchar"),
                    DatabaseColumnInfo(name="parameter_id", data_type="varchar"),
                    DatabaseColumnInfo(name="value", data_type="double"),
                ],
                default_time_column="timestamp",
            )
        return schema_csv_bytes, DatabaseTableInfo(
            schema_name="aircraft_monitor",
            table_name="schema_mapping",
            columns=[
                DatabaseColumnInfo(name="table_name", data_type="varchar"),
                DatabaseColumnInfo(name="column_name", data_type="varchar"),
                DatabaseColumnInfo(name="data_type", data_type="varchar"),
            ],
        )

    monkeypatch.setattr("app.api.agent.fetch_table_csv", fake_fetch)
    monkeypatch.setattr(
        "app.services.parsing_agent.agent.build_llm_client",
        lambda settings: _StubLLMClient(),
    )
    payload = {
        "connection_id": "dbc_test",
        "max_rows": 500,
        "tables": [
            {
                "schema_name": "aircraft_monitor",
                "table_name": "engine_readings",
                "time_column": "timestamp",
            },
            {
                "schema_name": "aircraft_monitor",
                "table_name": "schema_mapping",
            },
        ],
    }
    with _client(tmp_path, monkeypatch) as client:
        started = client.post(f"{API}/runs/from-database", json=payload)
        assert started.status_code == 202, started.text
        run_id = started.json()["run_id"]
        run = client.get(f"{API}/runs/{run_id}").json()
        result = client.get(f"{API}/runs/{run_id}/result").json()

    assert run["status"] == "COMPLETED"
    assert len(result["reports"]) == 2
    assert all(
        report["resource_type"] == "database_table" for report in result["reports"]
    )
    assert all(
        report["source"]["source_type"] == "database"
        for report in result["reports"]
    )


def test_database_connection_rejects_more_than_500_rows(tmp_path, monkeypatch):
    payload = {
        "connection_id": "dbc_test",
        "max_rows": 501,
        "tables": [{"table_name": "engine_readings"}],
    }
    with _client(tmp_path, monkeypatch) as client:
        response = client.post(f"{API}/runs/from-database", json=payload)
    assert response.status_code == 422


def test_database_connection_response_never_contains_password(monkeypatch):
    def fake_connect(request, *, settings):
        return DatabaseConnectionResponse(
            connection_id="dbc_test",
            database_name=request.database,
            tables=[
                DatabaseTableInfo(
                    schema_name=request.database,
                    table_name="sensor_reading",
                    columns=[
                        DatabaseColumnInfo(name="timestamp", data_type="datetime")
                    ],
                    default_time_column="timestamp",
                )
            ],
            expires_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr("app.api.database.connect_and_discover", fake_connect)
    from app.main import app

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/database/connections",
            json={
                "host": "127.0.0.1",
                "port": 3306,
                "username": "reader",
                "password": "do-not-return",
                "database": "aircraft_monitor",
            },
        )
    assert response.status_code == 200
    assert "do-not-return" not in response.text
    assert response.json()["tables"][0]["table_name"] == "sensor_reading"


def test_legacy_business_routes_are_removed(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        assert client.post("/api/v1/timeseries/analyze").status_code == 404
        assert client.post("/api/v1/aircraft/analyze").status_code == 404
        assert client.post("/api/v1/batches").status_code == 404


def test_agent_requires_llm_configuration(tmp_path, monkeypatch):
    from app.config import Settings

    monkeypatch.setattr(
        "app.api.agent.get_settings",
        lambda: Settings(
            agent_runs_dir=tmp_path / "agent_runs",
            llm_enable_api=True,
            llm_required=True,
            llm_api_base=None,
            llm_api_key=None,
            llm_model=None,
        ),
    )
    with _client(tmp_path, monkeypatch) as client, ExitStack() as stack:
        uploads = []
        for path in (SCHEMA, ENGINE):
            handle = stack.enter_context(path.open("rb"))
            uploads.append(("files", (path.name, handle, "application/octet-stream")))
        started = client.post(f"{API}/runs", data={}, files=uploads)
        assert started.status_code == 202
        run = client.get(f"{API}/runs/{started.json()['run_id']}").json()
    assert run["status"] == "FAILED"
    assert "LLM 未配置" in run["error"]


def test_openai_compatible_client_returns_text_and_object_attributes():
    content = {
        "summary": "综合分析完成",
        "overall_status": "WATCH",
        "attributes": [
            {
                "entity_ref": "C919-SYN-001.engine_left",
                "attribute_name": "maintenance_advice",
                "value": "建议在下一次航后检查排气温度传感器。",
                "description": "文本型维修建议",
                "category": "recommendation",
                "confidence": 0.88,
            },
            {
                "attribute_name": "status_statistics",
                "value": {"normal": 18, "watch": 9},
                "description": "状态统计",
                "category": "summary",
                "confidence": 0.95,
            },
            *[
                {
                    "attribute_name": f"extra_observation_{index}",
                    "value": f"补充观察 {index}",
                    "description": "用于验证属性数量压缩。",
                    "category": "supplementary",
                    "confidence": 0.7,
                }
                for index in range(1, 6)
            ],
        ],
        "recommendations": ["持续监控"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(content, ensure_ascii=False)
                        }
                    }
                ]
            },
        )

    client = OpenAICompatibleLLMClient(
        api_base="https://example.invalid/v1",
        api_key="test",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )
    result = client.analyze({"time_series_results": []})
    client.close()

    assert result.model == "test-model"
    assert len(result.attributes) == 5
    assert result.attributes[0].value.startswith("建议")
    assert result.attributes[1].value == {"normal": 18, "watch": 9}
    assert result.attributes[-1].attribute_name == "其他补充观察"


def test_openai_compatible_client_retries_invalid_json():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        content = (
            "not-json"
            if calls == 1
            else json.dumps(
                {
                    "summary": "重试后成功",
                    "overall_status": "NORMAL",
                    "attributes": [
                        {
                            "attribute_name": "retry_status",
                            "value": "ok",
                            "confidence": 0.9,
                        }
                    ],
                    "recommendations": [],
                },
                ensure_ascii=False,
            )
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": content},
                    }
                ]
            },
        )

    client = OpenAICompatibleLLMClient(
        api_base="https://example.invalid/v1",
        api_key="test",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )
    result = client.analyze({"resource": {"resource_name": "demo.csv"}})
    client.close()

    assert calls == 2
    assert result.summary == "重试后成功"


def test_resource_llm_failure_degrades_without_failing_batch(
    tmp_path,
    monkeypatch,
):
    class _PartiallyFailingClient(_StubLLMClient):
        def analyze(self, payload):
            if payload["resource"]["resource_name"] == ENGINE.name:
                raise LLMResponseError("LLM 未返回合法 JSON。")
            return super().analyze(payload)

    monkeypatch.setattr(
        "app.services.parsing_agent.agent.build_llm_client",
        lambda settings: _PartiallyFailingClient(),
    )
    with _client(tmp_path, monkeypatch) as client, ExitStack() as stack:
        uploads = []
        for path in (ENGINE, HYDRAULIC):
            handle = stack.enter_context(path.open("rb"))
            uploads.append(("files", (path.name, handle, "text/csv")))
        started = client.post(f"{API}/runs", files=uploads)
        assert started.status_code == 202, started.text
        run_id = started.json()["run_id"]
        run = client.get(f"{API}/runs/{run_id}").json()
        result = client.get(f"{API}/runs/{run_id}/result").json()

    assert run["status"] == "COMPLETED"
    assert len(result["reports"]) == 2
    degraded = next(
        report
        for report in result["reports"]
        if report["resource_name"] == ENGINE.name
    )
    assert degraded["attributes"][0]["model_version"] == "fallback"
    assert any(
        "已保留确定性解析结果" in warning
        for warning in degraded["processing"]["warnings"]
    )
    assert any(
        item["status"] == "degraded"
        for item in result["tool_trace"]
    )


def test_run_history_lists_and_hides_completed_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.parsing_agent.agent.build_llm_client",
        lambda settings: _StubLLMClient(),
    )
    with _client(tmp_path, monkeypatch) as client, ENGINE.open("rb") as handle:
        started = client.post(
            f"{API}/runs",
            files=[("files", (ENGINE.name, handle, "text/csv"))],
        )
        assert started.status_code == 202, started.text
        run_id = started.json()["run_id"]
        assert client.get(f"{API}/runs/{run_id}").json()["status"] == "COMPLETED"

        history = client.get(f"{API}/runs").json()
        assert history[0]["run_id"] == run_id
        assert history[0]["label"] == ENGINE.name
        assert history[0]["status"] == "COMPLETED"

        hidden = client.post(f"{API}/runs/{run_id}/hide")
        assert hidden.status_code == 200
        assert client.get(f"{API}/runs").json() == []


def test_model_settings_api_never_returns_api_key(tmp_path, monkeypatch):
    from app.services.runtime_model_config import RuntimeModelConfig

    runtime = RuntimeModelConfig(tmp_path / "model-config.json")
    runtime.save_verified(
        "https://example.invalid/v1",
        "test-model",
        "secret-key-value",
    )
    monkeypatch.setattr(
        "app.api.settings.get_runtime_model_config",
        lambda: runtime,
    )
    with _client(tmp_path, monkeypatch) as client:
        response = client.get("/api/v1/settings/model")
    assert response.status_code == 200
    assert response.json()["configured"] is True
    assert response.json()["model"] == "test-model"
    assert response.json()["key_masked"] == "********"
    assert "secret-key-value" not in response.text
