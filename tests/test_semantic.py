"""语义层与脱敏单元测试：规则命中、歧义判定、模型降级、人工描述优先级。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Settings
from app.services.masking import format_pattern, mask_examples, mask_value
from app.services.pipeline import _build_fields
from app.services.semantic import FieldContext
from app.services.semantic.base import FieldSemantics, ResourceContext
from app.services.semantic.openai_compatible import OpenAICompatibleProvider
from app.services.semantic.rule_based import RuleBasedProvider
from app.services.sql_parser import parse_sql

SEMANTIC_SQL = """
SELECT
    c.cust_no       AS cust_no,
    c.customer_name AS customer_name,
    o.amount        AS amt,
    p.product_name  AS product_name,
    o.order_id      AS order_id
FROM dwd.customer c
JOIN dwd.order o ON c.id = o.customer_id
JOIN dwd.product p ON o.product_id = p.id
"""


@pytest.fixture()
def parsed():
    result = parse_sql(SEMANTIC_SQL, "mysql")
    assert result.ok, result.error
    return result


# ---------------------------------------------------------------- 脱敏


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("CUST0001", "C***01"),          # 方案 §5 示例
        ("C0000001", "C***01"),
        ("AB", "A***"),                   # 过短时只保留首字符
        ("张三", "张***"),
        ("吕桂英", "吕***"),               # 三字姓名不得暴露后两字
        ("北京市朝阳区", "北***阳区"),      # 长串保留首字符与末两位
        ("customer@example.com", "c***@example.com"),
        ("13812345678", "138****5678"),   # 手机号保留号段
        ("123456", "12**56"),
        ("1234", "****"),                 # 短数字全部打码
        ("1234.56", "****.**"),           # 数值保留小数位形状
        ("-42.5", "-**.*"),
        ("2024-03-15", "2024-03-15"),     # 日期保留，供语义判断
        (None, None),
    ],
)
def test_mask_value(raw, expected):
    assert mask_value(raw) == expected


def test_mask_examples_dedupes_and_limits():
    values = ["CUST0001", "CUST0002", "CUST0003", None, "", float("nan")]
    examples = mask_examples(values, limit=2)
    assert len(examples) == 2
    assert all("***" in e for e in examples)


def test_format_pattern_collapses_runs():
    assert format_pattern("CUST-0001") == "A-9"
    assert format_pattern("2024-03-15") == "9-9-9"
    assert format_pattern("abcdef") == "a"          # 连续同类字符折叠为一个符号
    assert format_pattern("C0000001") == "A9"
    # 原文就是规范模式时返回 None，避免把噪音当成格式特征
    assert format_pattern("A-9") is None


# ---------------------------------------------------------------- 规则提供方


def test_rule_provider_known_abbreviations():
    provider = RuleBasedProvider()

    cust_no = provider.analyze_field(FieldContext(
        resource_code="r", resource_description=None, field_name="cust_no",
        sql_expression="c.cust_no", physical_type="string", user_description=None,
    ))
    assert cust_no.display_name == "客户编号"
    assert cust_no.business_type == "customer_code"
    assert cust_no.confidence >= 0.85
    assert cust_no.candidates == []

    # 业务编码与内部主键应区分开
    cust_id = provider.analyze_field(FieldContext(
        resource_code="r", resource_description=None, field_name="cust_id",
        sql_expression="c.id", physical_type="integer", user_description=None,
    ))
    assert cust_id.display_name == "客户标识"
    assert cust_id.business_type == "customer_identifier"

    amt = provider.analyze_field(FieldContext(
        resource_code="r", resource_description=None, field_name="amt",
        sql_expression="o.amount", physical_type="number", user_description=None,
    ))
    assert amt.display_name == "金额"
    assert amt.unit == "元"

    order_id = provider.analyze_field(FieldContext(
        resource_code="r", resource_description=None, field_name="order_id",
        sql_expression="o.order_id", physical_type="string", user_description=None,
    ))
    assert order_id.display_name == "订单编号"


def test_rule_provider_flags_ambiguity():
    """两个规则并列命中时应给出候选，且置信度落到阈值以下（方案 §4.3）。"""
    provider = RuleBasedProvider()
    result = provider.analyze_field(FieldContext(
        resource_code="r", resource_description=None, field_name="product_name",
        sql_expression="p.product_name", physical_type="string", user_description=None,
    ))

    assert set(result.candidates) == {"产品", "名称"}
    assert result.confidence < 0.85
    # 候选并列时不武断给出名称与业务类型
    assert result.display_name is None
    assert result.business_type is None


def test_rule_provider_unknown_field_is_low_confidence():
    provider = RuleBasedProvider()
    result = provider.analyze_field(FieldContext(
        resource_code="r", resource_description=None, field_name="xyzzy",
        sql_expression="t.xyzzy", physical_type="string", user_description=None,
    ))
    assert result.confidence == 0.0
    assert result.display_name is None
    assert result.business_type is None


def test_rule_provider_is_deterministic():
    """规则实现必须可复现，同一输入两次结果一致。"""
    provider = RuleBasedProvider()
    context = FieldContext(
        resource_code="r", resource_description=None, field_name="cust_no",
        sql_expression="c.cust_no", physical_type="string", user_description=None,
    )
    assert provider.analyze_field(context) == provider.analyze_field(context)


def test_rule_provider_resource_semantics():
    provider = RuleBasedProvider()
    result = provider.analyze_resource(ResourceContext(
        resource_code="customer_profile",
        resource_description=None,
        source_tables=["dwd.customer", "dwd.order"],
        field_names=["customer_id"],
        has_aggregation=True,
        row_count=1232,
    ))
    assert result.name == "Customer Profile"
    assert "汇总" in result.tags
    assert "多表关联" in result.tags


# ---------------------------------------------------------------- 模型提供方


def _provider_with_response(content: str, status_code: int = 200) -> OpenAICompatibleProvider:
    """构造一个用 MockTransport 返回固定内容的提供方。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = {"choices": [{"message": {"content": content}}]}
        return httpx.Response(status_code, json=body)

    return OpenAICompatibleProvider(
        api_base="https://example.invalid/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )


def _field_context() -> FieldContext:
    return FieldContext(
        resource_code="customer_profile",
        resource_description="面向营销分析的客户基础信息与交易行为汇总。",
        field_name="cust_no",
        sql_expression="c.cust_no",
        physical_type="string",
        user_description="客户的业务唯一编号。",
        source_tables=["dwd.customer"],
        masked_examples=["C***01", "C***02"],
        statistics={"null_rate": 0.0, "distinct_rate": 1.0},
    )


def test_openai_provider_parses_fixed_json():
    payload = {
        "display_name": "客户编号",
        "description": "用于唯一标识客户的业务编号。",
        "business_type": "customer_identifier",
        "tags": ["客户", "标识"],
        "unit": None,
        "confidence": 0.93,
    }
    provider = _provider_with_response(json.dumps(payload, ensure_ascii=False))
    result = provider.analyze_field(_field_context())

    assert result.display_name == "客户编号"
    assert result.business_type == "customer_identifier"
    assert result.tags == ["客户", "标识"]
    assert result.unit is None
    assert result.confidence == 0.93
    assert result.provider == "openai_compatible"
    provider.close()


def test_openai_provider_tolerates_fenced_json():
    fenced = '```json\n{"display_name": "客户编号", "confidence": 0.9}\n```'
    provider = _provider_with_response(fenced)
    result = provider.analyze_field(_field_context())
    assert result.display_name == "客户编号"
    assert result.confidence == 0.9
    provider.close()


def test_provider_degrades_on_garbage():
    """模型返回非 JSON 时必须降级为无补全，而不是抛异常。"""
    provider = _provider_with_response("抱歉，我无法判断这个字段。")
    result = provider.analyze_field(_field_context())
    assert result.confidence == 0.0
    assert result.display_name is None
    provider.close()


def test_provider_degrades_on_server_error():
    provider = _provider_with_response("{}", status_code=500)
    result = provider.analyze_field(_field_context())
    assert result.confidence == 0.0
    provider.close()


def test_provider_does_not_retry_client_errors():
    """4xx 属于请求本身不合法，重试没有意义，应只调用一次。"""
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(400, json={"error": "bad request"})

    provider = OpenAICompatibleProvider(
        api_base="https://example.invalid/v1", api_key="k", model="m",
        transport=httpx.MockTransport(handler),
    )
    assert provider.analyze_field(_field_context()).confidence == 0.0
    assert calls["count"] == 1
    provider.close()


def test_provider_clamps_confidence_and_cleans_tags():
    payload = {
        "display_name": "  客户编号  ",
        "tags": ["客户", "客户", "", None, "标识"],
        "unit": "N/A",
        "confidence": 7.5,
    }
    provider = _provider_with_response(json.dumps(payload, ensure_ascii=False))
    result = provider.analyze_field(_field_context())

    assert result.display_name == "客户编号"      # 两端空白被清理
    assert result.tags == ["客户", "标识"]         # 去重且剔除空值
    assert result.unit is None                    # "N/A" 视为缺失
    assert result.confidence == 1.0               # 越界值被夹紧
    provider.close()


def test_provider_sends_only_metadata_not_raw_rows():
    """入参必须只含元数据与脱敏样例（方案 §4.4）。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    provider = OpenAICompatibleProvider(
        api_base="https://example.invalid/v1", api_key="k", model="m",
        transport=httpx.MockTransport(handler),
    )
    provider.analyze_field(_field_context())

    sent = json.dumps(captured, ensure_ascii=False)
    assert "C***01" in sent          # 脱敏样例可用
    assert "客户的业务唯一编号。" in sent  # 人工描述可用
    assert captured["model"] == "m"
    # 字段级 payload 结构
    user_payload = json.loads(captured["messages"][1]["content"])
    assert user_payload["field"]["name"] == "cust_no"
    assert user_payload["field"]["sql_expression"] == "c.cust_no"
    assert user_payload["profile"]["masked_examples"] == ["C***01", "C***02"]
    provider.close()


# ---------------------------------------------------------------- 合并优先级


class _StubProvider:
    """返回固定语义结果，用于验证合并规则。"""

    name = "stub"

    def __init__(self, semantics: FieldSemantics) -> None:
        self._semantics = semantics

    def analyze_field(self, context: FieldContext) -> FieldSemantics:
        return self._semantics

    def analyze_resource(self, context: ResourceContext):
        raise NotImplementedError

    def close(self) -> None:
        pass


def test_user_description_wins_over_model(parsed):
    """人工描述优先，模型不得覆盖（方案 §4.4）。"""
    provider = _StubProvider(FieldSemantics(
        display_name="客户编号", description="模型生成的描述。",
        business_type="customer_identifier", tags=["客户"], confidence=0.99,
    ))

    fields = _build_fields(
        parse_result=parsed,
        profile_result=None,
        field_descriptions={"cust_no": "人工填写的字段描述。"},
        resource_description=None,
        resource_code="r",
        provider=provider,
        settings=Settings(semantic_enable_api=False),
    )
    cust_no = next(f for f in fields if f.name == "cust_no")

    assert cust_no.description == "人工填写的字段描述。"
    assert cust_no.description_source == "user"
    # 模型仍可补充名称与标签
    assert cust_no.display_name == "客户编号"
    assert cust_no.tags == ["客户"]


def test_model_used_when_no_user_description(parsed):
    provider = _StubProvider(FieldSemantics(
        display_name="客户编号", description="模型生成的描述。",
        business_type="customer_identifier", tags=["客户"], confidence=0.99,
    ))

    fields = _build_fields(
        parse_result=parsed, profile_result=None, field_descriptions={},
        resource_description=None, resource_code="r",
        provider=provider, settings=Settings(semantic_enable_api=False),
    )
    cust_no = next(f for f in fields if f.name == "cust_no")

    assert cust_no.description == "模型生成的描述。"
    assert cust_no.description_source == "model"
    assert cust_no.semantic_status.value == "CONFIRMED"


def test_low_confidence_marks_pending(parsed):
    """置信度低于 0.85 时保留待确认状态（方案 §4.3）。"""
    provider = _StubProvider(FieldSemantics(confidence=0.6))

    fields = _build_fields(
        parse_result=parsed, profile_result=None, field_descriptions={},
        resource_description=None, resource_code="r",
        provider=provider, settings=Settings(semantic_enable_api=False),
    )
    assert all(f.semantic_status.value == "PENDING" for f in fields)


def test_user_description_resolves_ambiguity(parsed):
    """人工描述已明确含义时，歧义即被消解，不再是待确认。"""
    provider = _StubProvider(FieldSemantics(confidence=0.4, candidates=["产品", "名称"]))

    fields = _build_fields(
        parse_result=parsed, profile_result=None,
        field_descriptions={"product_name": "商品名称。"},
        resource_description=None, resource_code="r",
        provider=provider, settings=Settings(semantic_enable_api=False),
    )
    product_name = next(f for f in fields if f.name == "product_name")
    assert product_name.semantic_status.value == "CONFIRMED"
    assert product_name.description == "商品名称。"


def test_fields_come_only_from_sql(parsed):
    """模型无法新增字段：字段集合恒等于 SQL 输出（方案 §4.4）。"""
    provider = _StubProvider(FieldSemantics(display_name="X", confidence=0.99))

    fields = _build_fields(
        parse_result=parsed, profile_result=None, field_descriptions={},
        resource_description=None, resource_code="r",
        provider=provider, settings=Settings(semantic_enable_api=False),
    )
    assert [f.name for f in fields] == [f.name for f in parsed.output_fields]
