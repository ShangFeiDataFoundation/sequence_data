"""端到端验收：走完上传 → 预览 → 填描述 → 解析 → 读结果（方案 §2）。

用临时目录作为 data_dir，不污染项目运行数据。

    .venv/Scripts/python.exe -m pytest tests/ -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SAMPLES = PROJECT_ROOT / "samples"
API = "/api/v1/batches"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个用例独立的存储目录与规则提供方（不依赖外部 API）。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "batches"))
    monkeypatch.setenv("SEMANTIC_ENABLE_API", "false")

    from app.config import get_settings

    get_settings.cache_clear()

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


def _upload(client: TestClient, sql_names: list[str], data_names: list[str]):
    files = [
        ("sql_files", (name, (SAMPLES / name).read_bytes(), "application/sql"))
        for name in sql_names
    ]
    files += [
        ("data_files", (name, (SAMPLES / name).read_bytes(), "text/csv"))
        for name in data_names
    ]
    return client.post(API, data={"sql_dialect": "mysql"}, files=files)


# ---------------------------------------------------------------- 用例


def test_full_flow(client: TestClient):
    """完整链路：上传 → PREVIEW_READY → 启动 → COMPLETED → 读结果。"""
    response = _upload(client, ["customer_profile.sql"], ["customer_profile.csv"])
    assert response.status_code == 201, response.text
    created = response.json()

    assert created["status"] == "PREVIEW_READY"
    assert created["unmatched_sql_files"] == []

    preview = created["resources"][0]
    assert preview["resource_code"] == "customer_profile"
    assert preview["data_file"] == "customer_profile.csv"
    assert preview["matched_by"] == "auto"
    assert preview["parse_error"] is None
    assert preview["output_fields"] == [
        "customer_id", "cust_no", "customer_name", "city",
        "gender", "age", "order_count", "total_amount", "last_order_at",
    ]

    batch_id = created["batch_id"]

    # 状态接口此时应为 PREVIEW_READY
    assert client.get(f"{API}/{batch_id}").json()["status"] == "PREVIEW_READY"

    # 填写描述并启动解析
    started = client.post(
        f"{API}/{batch_id}/start",
        json={
            "resource_descriptions": {
                "customer_profile.sql": "面向营销分析的客户基础信息与交易行为汇总。"
            },
            "field_descriptions": {
                "customer_profile.sql": {
                    "customer_id": "客户的业务唯一编号。",
                    "total_amount": "客户累计支付金额，单位为元。",
                }
            },
        },
    )
    assert started.status_code == 200, started.text

    final = client.get(f"{API}/{batch_id}").json()
    assert final["status"] == "COMPLETED", final
    assert final["completed_resources"] == 1
    assert final["resources"][0]["field_count"] == 9

    # 读取单份结果
    result = client.get(f"{API}/{batch_id}/results/customer_profile")
    assert result.status_code == 200, result.text
    descriptor = result.json()

    assert descriptor["resource_code"] == "customer_profile"
    assert descriptor["source_tables"] == ["dwd.customer", "dwd.order"]
    assert descriptor["description"] == "面向营销分析的客户基础信息与交易行为汇总。"

    summary = descriptor["sql_summary"]
    assert summary["dialect"] == "mysql"
    assert summary["has_aggregation"] is True
    assert summary["joins"][0]["type"] == "LEFT"
    assert summary["joins"][0]["left_field"] == "c.id"
    assert summary["joins"][0]["right_field"] == "p.customer_id"
    assert summary["ctes"] == ["paid_order"]
    assert set(summary["aggregate_functions"]) == {"COUNT", "SUM", "MAX"}
    assert summary["filters"] == ["c.status = 'active'"]
    # 分区条件写在 CTE 内部，也应被识别
    assert any("o.dt" in f for f in summary["partition_filters"])

    # 数据画像：行数应等于 CSV 数据行数，且不触发小样本告警
    profile = descriptor["profile"]
    assert profile["row_count"] == 1232
    assert profile["sample_size_warning"] is False
    assert profile["data_file"] == "customer_profile.csv"
    assert profile["missing_columns"] == []
    assert profile["unmatched_columns"] == []

    fields = {f["name"]: f for f in descriptor["fields"]}

    # 人工描述优先，且标注来源
    assert fields["customer_id"]["description"] == "客户的业务唯一编号。"
    assert fields["customer_id"]["description_source"] == "user"
    assert fields["total_amount"]["description"] == "客户累计支付金额，单位为元。"

    # 血缘穿透 CTE
    assert fields["total_amount"]["source_fields"] == ["dwd.order.amount"]
    assert fields["order_count"]["source_fields"] == ["dwd.order.order_id"]
    assert fields["customer_name"]["source_fields"] == ["dwd.customer.customer_name"]

    # 画像统计
    assert fields["customer_id"]["data_type"] == "integer"
    assert fields["customer_id"]["statistics"]["null_rate"] == 0
    assert fields["customer_id"]["statistics"]["is_identifier_candidate"] is True
    assert fields["total_amount"]["data_type"] == "number"
    assert fields["total_amount"]["statistics"]["max"] > 0
    assert fields["last_order_at"]["data_type"] == "datetime"

    # age 有约 8% 空值
    assert 0.05 < fields["age"]["statistics"]["null_rate"] < 0.12

    # city 是低基数字段，应给出枚举候选与高频值
    assert fields["city"]["statistics"]["enum_candidates"]
    assert fields["city"]["statistics"]["top_values"][0]["count"] > 0

    # 样例必须已脱敏：客户编号与姓名都不能出现完整原值
    assert fields["cust_no"]["examples"], "cust_no 应有脱敏样例"
    for example in fields["cust_no"]["examples"]:
        assert "***" in example
    assert all("*" in e for e in fields["customer_name"]["examples"])

    # 资源级信息
    assert descriptor["tags"], "应至少聚合出资源标签"
    assert descriptor["name"]

    # 全部结果接口
    all_results = client.get(f"{API}/{batch_id}/results").json()
    assert all_results["status"] == "COMPLETED"
    assert len(all_results["results"]) == 1


def test_multi_resource_batch(client: TestClient):
    """一次上传多份 SQL 与数据文件，各自独立产出结果。"""
    response = _upload(
        client,
        ["customer_profile.sql", "order_daily.sql"],
        ["customer_profile.csv", "order_daily.csv"],
    )
    assert response.status_code == 201, response.text
    created = response.json()
    batch_id = created["batch_id"]

    codes = {r["resource_code"] for r in created["resources"]}
    assert codes == {"customer_profile", "order_daily"}
    assert created["unmatched_data_files"] == []
    assert created["unmatched_sql_files"] == []

    assert client.post(f"{API}/{batch_id}/start", json={}).status_code == 200
    final = client.get(f"{API}/{batch_id}").json()
    assert final["status"] == "COMPLETED"
    assert final["completed_resources"] == 2

    results = client.get(f"{API}/{batch_id}/results").json()["results"]
    by_code = {r["resource_code"]: r for r in results}

    daily = by_code["order_daily"]
    assert daily["source_tables"] == ["dwd.order"]
    assert daily["profile"]["row_count"] == 1930
    assert daily["sql_summary"]["has_aggregation"] is True
    assert daily["sql_summary"]["joins"] == []

    stat_date = next(f for f in daily["fields"] if f["name"] == "stat_date")
    assert stat_date["data_type"] == "datetime"
    assert stat_date["statistics"]["time_span_days"] > 700


def test_manual_mapping_fixes_auto_pairing(client: TestClient):
    """自动配对错误时，可用 mappings 手工纠正。"""
    files = [
        ("sql_files", ("customer_profile.sql", (SAMPLES / "customer_profile.sql").read_bytes(), "application/sql")),
        ("data_files", ("order_daily.csv", (SAMPLES / "order_daily.csv").read_bytes(), "text/csv")),
    ]
    created = client.post(API, data={"sql_dialect": "mysql"}, files=files).json()
    batch_id = created["batch_id"]

    # 主名不同，自动配对应失败
    assert created["resources"][0]["data_file"] is None
    assert created["resources"][0]["matched_by"] == "none"
    assert created["unmatched_data_files"] == ["order_daily.csv"]

    # 手工指定映射后启动
    started = client.post(
        f"{API}/{batch_id}/start",
        json={"mappings": {"customer_profile.sql": "order_daily.csv"}},
    )
    assert started.status_code == 200, started.text

    result = client.get(f"{API}/{batch_id}/results/customer_profile").json()
    assert result["profile"]["data_file"] == "order_daily.csv"
    assert result["profile"]["row_count"] == 1930
    # SQL 字段与数据表头对不上，应被记录而不是静默通过
    assert result["profile"]["missing_columns"]
    assert result["profile"]["unmatched_columns"]


def test_small_sample_warns(tmp_path, monkeypatch):
    """少于 1000 行时应标记 sample_size_warning（方案 §3.3）。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "batches"))
    monkeypatch.setenv("SEMANTIC_ENABLE_API", "false")

    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import app

    rows = "\n".join(
        f"{i},C{i:07d},测试客户,北京,M,30,1,100.00,2024-01-01 10:00:00" for i in range(1, 11)
    )
    header = "customer_id,cust_no,customer_name,city,gender,age,order_count,total_amount,last_order_at"
    small_csv = f"{header}\n{rows}\n".encode()

    with TestClient(app) as client:
        created = client.post(
            API,
            data={"sql_dialect": "mysql"},
            files=[
                ("sql_files", ("customer_profile.sql", (SAMPLES / "customer_profile.sql").read_bytes(), "application/sql")),
                ("data_files", ("customer_profile.csv", small_csv, "text/csv")),
            ],
        ).json()
        batch_id = created["batch_id"]
        client.post(f"{API}/{batch_id}/start", json={})
        result = client.get(f"{API}/{batch_id}/results/customer_profile").json()

    assert result["profile"]["row_count"] == 10
    assert result["profile"]["sample_size_warning"] is True
    get_settings.cache_clear()


def test_rejects_bad_input(client: TestClient):
    """方言、扩展名、空文件等非法输入应返回明确错误。"""
    sql = ("sql_files", ("a.sql", b"SELECT 1", "application/sql"))
    csv = ("data_files", ("a.csv", b"x\n1\n", "text/csv"))

    assert client.post(API, data={"sql_dialect": "oracle9i"}, files=[sql, csv]).status_code == 400
    assert client.post(API, data={"sql_dialect": "mysql"}, files=[("sql_files", ("a.txt", b"SELECT 1", "text/plain")), csv]).status_code == 400
    assert client.post(API, data={"sql_dialect": "mysql"}, files=[("sql_files", ("a.sql", b"SELECT 1", "application/sql")), ("data_files", ("a.docx", b"x", "application/msword"))]).status_code == 400
    assert client.post(API, data={"sql_dialect": "mysql"}, files=[sql, ("data_files", ("a.csv", b"", "text/csv"))]).status_code == 400
    assert client.post(API, data={"sql_dialect": "mysql"}, files=[sql]).status_code == 400

    missing = client.get(f"{API}/bat_does_not_exist")
    assert missing.status_code == 404
    assert missing.json()["detail"]


def test_sql_parse_error_surfaces_in_preview(client: TestClient):
    """SQL 语法错误应在预览阶段暴露，而不是等到解析时。"""
    created = client.post(
        API,
        data={"sql_dialect": "mysql"},
        files=[
            ("sql_files", ("broken.sql", b"SELECT FROM WHERE", "application/sql")),
            ("data_files", ("broken.csv", b"customer_id\n1\n", "text/csv")),
        ],
    ).json()

    preview = created["resources"][0]
    assert preview["parse_error"]
    assert preview["output_fields"] == []

    # 启动解析后该资源失败，批次整体标记 FAILED
    batch_id = created["batch_id"]
    client.post(f"{API}/{batch_id}/start", json={})
    final = client.get(f"{API}/{batch_id}").json()
    assert final["status"] == "FAILED"
    assert final["failed_resources"] == 1
    assert final["resources"][0]["error"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
