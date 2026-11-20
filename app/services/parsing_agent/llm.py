"""调用 LLM，将时序统计特征与表结构信息转换为最终结构化结果。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol

import httpx

from ...config import Settings
from ...schemas import (
    AgentAttribute,
    AgentLLMResult,
    TimeSeriesDecisionStatus,
)
from ...storage import now
from ..runtime_model_config import get_runtime_model_config

logger = logging.getLogger(__name__)

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
MAX_RESOURCE_ATTRIBUTES = 5

SYSTEM_PROMPT = """你是飞机时序监测与结构化数据语义分析专家。
每次输入只描述一个资源，可能是：
1. 时序数据的统计特征；
2. 普通结构化数据表的字段画像；
3. SQL、JSON、CSV 或 Excel 中的表结构定义。

请只针对当前资源生成结构化结果。统计特征和字段画像只是证据，不要简单复述。
可以生成多个属性，属性值允许是：
- 字符串描述
- 数值
- 布尔值
- 数组
- 结构化对象

必须只输出一个 JSON 对象，格式如下：
{
  "summary": "整体分析摘要",
  "overall_status": "NORMAL/WATCH/POSSIBLE_FAULT/HIGH_RISK/UNKNOWN",
  "attributes": [
    {
      "entity_ref": "实体引用，可为空",
      "attribute_name": "属性名称",
      "value": "属性值，允许文本或结构化值",
      "description": "属性解释",
      "category": "属性分类",
      "business_type": "业务类型，可为空",
      "unit": "单位，可为空",
      "confidence": 0.0,
      "source_files": ["来源文件"],
      "source_refs": ["证据中的实体/指标引用"],
      "evidence": ["支撑该属性的推理证据"]
    }
  ],
  "recommendations": ["建议"]
}

要求：
1. 属性必须精简，通常输出3到5条，绝对不要超过5条；
2. 时序资源优先合并为：数据质量、实体健康与异常概览、趋势周期与相关性、风险建议；
3. 普通结构化数据表优先合并为：字段语义、数据质量、业务对象与维度、综合判断；
4. 表结构资源优先合并为：业务实体、字段语义、主外键与关联关系、结构风险；
5. 同一类别、同一实体或同一事件的多个异常必须合并到一条属性中，禁止每个异常窗口单独生成一条属性；
6. 每条属性的 description 控制在120字以内，value 可以是简洁文本或聚合后的对象/数组；
7. 不要输出其他资源的结论，除非是在解释当前资源与关联表的关系；
8. 不确定时必须表述为“可能”“疑似”“证据不足”，不能给确定性故障结论；
9. 不能引入输入中不存在的飞机、传感器、参数或指标；
10. 每条属性尽量保留关键来源证据，但不要堆砌完整统计过程。"""


class LLMAnalysisClient(Protocol):
    provider: str
    model: str

    def analyze(self, payload: dict[str, Any]) -> AgentLLMResult: ...

    def close(self) -> None: ...


class LLMResponseError(RuntimeError):
    pass


class OpenAICompatibleLLMClient:
    provider = "openai_compatible"
    max_attempts = 2

    def __init__(
        self,
        *,
        api_base: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
        max_output_tokens: int = 8192,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.endpoint = _chat_completions_url(api_base)
        self.client = httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            transport=transport,
        )

    def analyze(self, payload: dict[str, Any]) -> AgentLLMResult:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            "temperature": 0,
            "max_tokens": self.max_output_tokens,
            "response_format": {"type": "json_object"},
        }
        last_error: LLMResponseError | None = None
        for attempt in range(self.max_attempts):
            try:
                response = self.client.post(self.endpoint, json=body)
                if response.status_code != 200:
                    raise LLMResponseError(
                        f"大模型 API 返回 HTTP {response.status_code}。"
                    )
                try:
                    choice = response.json()["choices"][0]
                    finish_reason = choice.get("finish_reason")
                    content = _message_content(choice.get("message") or {})
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    raise LLMResponseError(
                        "大模型响应结构不兼容。"
                    ) from exc
                if finish_reason == "length":
                    raise LLMResponseError("大模型输出被截断。")
                if not content.strip():
                    raise LLMResponseError("大模型返回了空内容。")
                parsed = _loads_json_object(content)
                if parsed is None:
                    raise LLMResponseError("LLM 未返回合法 JSON。")
                return _to_result(
                    parsed,
                    provider=self.provider,
                    model=self.model,
                )
            except LLMResponseError as exc:
                last_error = exc
                if attempt + 1 >= self.max_attempts:
                    break
                logger.warning(
                    "模型输出无效，执行第 %s 次重试：%s",
                    attempt + 2,
                    exc,
                )
            except httpx.HTTPError as exc:
                last_error = LLMResponseError(
                    "大模型网络请求失败或超时。"
                )
                last_error.__cause__ = exc
                if attempt + 1 >= self.max_attempts:
                    break
                logger.warning(
                    "模型网络请求失败，执行第 %s 次重试。",
                    attempt + 2,
                )
        raise last_error or LLMResponseError("大模型分析失败。")

    def close(self) -> None:
        self.client.close()


def build_llm_client(settings: Settings) -> LLMAnalysisClient | None:
    if not settings.llm_enable_api:
        return None
    credentials = get_runtime_model_config().effective(settings)
    if not credentials.configured:
        return None
    return OpenAICompatibleLLMClient(
        api_base=credentials.base_url,
        api_key=credentials.api_key,
        model=credentials.model,
        timeout=settings.llm_timeout_seconds,
        max_output_tokens=settings.llm_max_output_tokens,
    )


def _message_content(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        if parts:
            return "\n".join(parts)
    reasoning = message.get("reasoning_content")
    return reasoning if isinstance(reasoning, str) else ""


def _to_result(
    payload: dict[str, Any],
    *,
    provider: str,
    model: str,
) -> AgentLLMResult:
    attributes: list[AgentAttribute] = []
    raw_attributes = payload.get("attributes")
    if not isinstance(raw_attributes, list):
        raise LLMResponseError("LLM 结果的 attributes 必须是数组。")
    for raw in raw_attributes:
        if not isinstance(raw, dict) or not raw.get("attribute_name"):
            continue
        attributes.append(
            AgentAttribute(
                entity_ref=_optional_text(raw.get("entity_ref")),
                attribute_name=str(raw["attribute_name"]),
                value=raw.get("value"),
                description=_optional_text(raw.get("description")),
                category=_optional_text(raw.get("category")),
                business_type=_optional_text(raw.get("business_type")),
                unit=_optional_text(raw.get("unit")),
                confidence=_confidence(raw.get("confidence")),
                decision=TimeSeriesDecisionStatus.AUTO_ACCEPTED,
                source_files=_text_list(raw.get("source_files")),
                source_refs=_text_list(raw.get("source_refs")),
                evidence=_text_list(raw.get("evidence")),
                model_version=model,
            )
        )
    attributes = _compact_attributes(attributes)
    return AgentLLMResult(
        summary=_optional_text(payload.get("summary")) or "LLM 未提供整体摘要。",
        overall_status=_optional_text(payload.get("overall_status")),
        attributes=attributes,
        recommendations=_text_list(payload.get("recommendations")),
        provider=provider,
        model=model,
        generated_at=now(),
    )


def _compact_attributes(attributes: list[AgentAttribute]) -> list[AgentAttribute]:
    """保证单资源属性数量可控，并将次要观察合并为一条。"""
    if len(attributes) <= MAX_RESOURCE_ATTRIBUTES:
        return attributes
    keep_count = MAX_RESOURCE_ATTRIBUTES - 1
    primary = attributes[:keep_count]
    remainder = attributes[keep_count:]
    source_files = list(
        dict.fromkeys(
            source
            for attribute in remainder
            for source in attribute.source_files
        )
    )
    source_refs = list(
        dict.fromkeys(
            source_ref
            for attribute in remainder
            for source_ref in attribute.source_refs
        )
    )
    evidence = list(
        dict.fromkeys(
            item
            for attribute in remainder
            for item in attribute.evidence
        )
    )[:20]
    primary.append(
        AgentAttribute(
            attribute_name="其他补充观察",
            value=[
                {
                    "attribute_name": attribute.attribute_name,
                    "value": attribute.value,
                    "category": attribute.category,
                    "confidence": attribute.confidence,
                }
                for attribute in remainder
            ],
            description=f"已合并其余{len(remainder)}条次要或重复观察。",
            category="补充观察",
            confidence=max((attribute.confidence for attribute in remainder), default=0.0),
            decision=TimeSeriesDecisionStatus.AUTO_ACCEPTED,
            source_files=source_files,
            source_refs=source_refs,
            evidence=evidence,
            model_version=remainder[-1].model_version,
        )
    )
    return primary


def _chat_completions_url(api_base: str) -> str:
    base = api_base.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _loads_json_object(content: str) -> dict[str, Any] | None:
    if not isinstance(content, str):
        return None
    text = _FENCE.sub("", content.strip())
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _optional_text(item)
        if text and text not in result:
            result.append(text)
    return result


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0
