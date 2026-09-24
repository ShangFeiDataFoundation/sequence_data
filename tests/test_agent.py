"""增强型 Agent 端到端测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SAMPLES = PROJECT_ROOT / "samples"
API = "/api/v1/batches"


class _CloudStubProvider:
    """模拟云端语义提供方。"""

    name = "openai_compatible"

    def __init__(self, confidence: float = 0.96, candidates: list[str] | None = None):
        self.confidence = confidence
        self.candidates = candidates or []
        self.field_calls = 0

    def analyze_field(self, context):
        from app.services.semantic.base import FieldSemantics

        self.field_calls += 1
        return FieldSemantics(
            display_name=f"{context.field_name} 业务名称",
            description=f"{context.field_name} 的云端语义描述。",
            business_type="cloud_semantic",
            tags=["云端语义"],
            confidence=self.confidence,
            candidates=self.candidates,
            provider=self.name,
        )

    def analyze_resource(self, context):
        from app.services.semantic.base import ResourceSemantics

        return ResourceSemantics(
            name="云端资源名称",
            description="云端资源描述。",
            tags=["云端资源"],
            confidence=self.confidence,
            provider=self.name,
        )

    def close(self):
        return None


def _http_cloud_provider(confidence: float = 0.96):
    """构造真实 OpenAICompatibleProvider，并用 MockTransport 模拟云端 HTTP。"""
    from app.services.semantic.openai_compatible import OpenAICompatibleProvider

    calls = {"field": 0, "resource": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system_prompt = body["messages"][0]["content"]
        if "字段元数据" in system_prompt:
            calls["field"] += 1
            payload = {
                "display_name": "云端字段名称",
                "description": "云端生成的字段描述。",
                "business_type": "cloud_semantic",
                "tags": ["云端语义"],
                "unit": None,
                "confidence": confidence,
            }
        else:
            calls["resource"] += 1
            payload = {
                "name": "云端资源名称",
                "description": "云端资源描述。",
                "tags": ["云端资源"],
                "confidence": confidence,
            }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps(payload, ensure_ascii=False)}}
                ]
            },
        )

    provider = OpenAICompatibleProvider(
        api_base="https://api.example.invalid/v1",
        api_key="test-key",
        model="test-cloud-model",
        transport=httpx.MockTransport(handler),
    )
    return provider, calls


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "batches"))
    monkeypatch.setenv("SEMANTIC_ENABLE_API", "true")
    monkeypatch.setenv("SEMANTIC_API_BASE", "https://api.example.invalid/v1")
    monkeypatch.setenv("SEMANTIC_API_KEY", "test-key")
    monkeypatch.setenv("SEMANTIC_MODEL", "test-cloud-model")
    monkeypatch.setenv("AGENT_REQUIRE_API", "false")

    from app.config import get_settings

    get_settings.cache_clear()

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


def _upload(client: TestClient):
    return client.post(
        API,
        data={"sql_dialect": "mysql"},
        files=[
            (
                "sql_files",
                (
                    "customer_profile.sql",
                    (SAMPLES / "customer_profile.sql").read_bytes(),
                    "application/sql",
                ),
            ),
            (
                "data_files",
                (
                    "customer_profile.csv",
                    (SAMPLES / "customer_profile.csv").read_bytes(),
                    "text/csv",
                ),
            ),
        ],
    )


def test_enhanced_agent_runs_with_cloud_provider(client: TestClient, monkeypatch):
    """高置信度云端结果应完成整条 Agent 流程。"""
    provider, calls = _http_cloud_provider()
    monkeypatch.setattr(
        "app.services.agent.orchestrator.build_provider",
        lambda settings: provider,
    )

    created = _upload(client)
    assert created.status_code == 201, created.text
    batch_id = created.json()["batch_id"]

    started = client.post(
        f"{API}/{batch_id}/agent-runs",
        json={
            "require_api": True,
            "resource_descriptions": {
                "customer_profile.sql": "客户画像汇总。",
            },
        },
    )
    assert started.status_code == 202, started.text

    run = client.get(f"{API}/{batch_id}/agent-runs/latest").json()
    assert run["status"] == "COMPLETED", run
    assert run["provider"] == "openai_compatible"
    assert run["model"] == "test-cloud-model"
    assert run["progress"] == 1.0
    assert run["review_items"] == []
    assert run["event_count"] >= 4
    assert calls["field"] > 0
    assert calls["resource"] == 1

    events = client.get(f"{API}/{batch_id}/agent-runs/latest/events").json()
    event_types = [event["event_type"] for event in events["events"]]
    assert event_types[0] == "AGENT_STARTED"
    assert "PROVIDER_SELECTED" in event_types
    assert "RESOURCE_COMPLETED" in event_types
    assert event_types[-1] == "AGENT_FINISHED"

    result = client.get(f"{API}/{batch_id}/results/customer_profile").json()
    assert result["semantic_provider"] == "openai_compatible"
    assert result["name"] == "云端资源名称"
    assert result["fields"][0]["description"] == "云端生成的字段描述。"


def test_enhanced_agent_marks_low_confidence_for_review(
    client: TestClient,
    monkeypatch,
):
    """低置信度字段应产出审查项，同时保留解析结果。"""
    provider = _CloudStubProvider(confidence=0.4, candidates=["客户", "标识"])
    monkeypatch.setattr(
        "app.services.agent.orchestrator.build_provider",
        lambda settings: provider,
    )

    batch_id = _upload(client).json()["batch_id"]
    response = client.post(
        f"{API}/{batch_id}/agent-runs",
        json={"require_api": True},
    )
    assert response.status_code == 202, response.text

    run = client.get(f"{API}/{batch_id}/agent-runs/latest").json()
    assert run["status"] == "REVIEW_REQUIRED", run
    assert run["review_items"]
    assert all(item["code"] == "FIELD_SEMANTICS_PENDING" for item in run["review_items"])

    result = client.get(f"{API}/{batch_id}/results/customer_profile")
    assert result.status_code == 200
    assert result.json()["fields"][0]["semantic_candidates"] == ["客户", "标识"]


def test_enhanced_agent_can_require_cloud_api(client: TestClient, monkeypatch):
    """require_api=true 时，规则降级不应被当作成功。"""
    from app.services.semantic import RuleBasedProvider

    monkeypatch.setattr(
        "app.services.agent.orchestrator.build_provider",
        lambda settings: RuleBasedProvider(),
    )

    batch_id = _upload(client).json()["batch_id"]
    response = client.post(
        f"{API}/{batch_id}/agent-runs",
        json={"require_api": True},
    )
    assert response.status_code == 202, response.text

    run = client.get(f"{API}/{batch_id}/agent-runs/latest").json()
    assert run["status"] == "FAILED", run
    assert "要求使用云端 API" in run["error"]

    batch = client.get(f"{API}/{batch_id}").json()
    assert batch["status"] == "FAILED"
    assert "要求使用云端 API" in batch["error"]


def test_partial_resource_failure_requires_review(client: TestClient, monkeypatch):
    """部分资源失败时，其余结果仍保留，但 Agent 必须要求审查。"""
    provider = _CloudStubProvider()
    monkeypatch.setattr(
        "app.services.agent.orchestrator.build_provider",
        lambda settings: provider,
    )

    created = client.post(
        API,
        data={"sql_dialect": "mysql"},
        files=[
            ("sql_files", ("broken.sql", b"SELECT FROM WHERE", "application/sql")),
            (
                "sql_files",
                (
                    "customer_profile.sql",
                    (SAMPLES / "customer_profile.sql").read_bytes(),
                    "application/sql",
                ),
            ),
            ("data_files", ("broken.csv", b"customer_id\n1\n", "text/csv")),
            (
                "data_files",
                (
                    "customer_profile.csv",
                    (SAMPLES / "customer_profile.csv").read_bytes(),
                    "text/csv",
                ),
            ),
        ],
    )
    batch_id = created.json()["batch_id"]

    response = client.post(
        f"{API}/{batch_id}/agent-runs",
        json={"require_api": True},
    )
    assert response.status_code == 202, response.text

    run = client.get(f"{API}/{batch_id}/agent-runs/latest").json()
    assert run["status"] == "REVIEW_REQUIRED", run
    assert any(item["code"] == "RESOURCE_FAILED" for item in run["review_items"])
    assert client.get(f"{API}/{batch_id}/results/customer_profile").status_code == 200
