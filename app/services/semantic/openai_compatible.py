"""兼容 OpenAI 协议的小模型接入（方案 §4.4）。

约束：模型只接收 FieldContext 组装出的字段名、SQL 表达式、来源表、类型、
统计摘要、脱敏样例和人工描述；必须返回固定 JSON 而非自由文本。
任何网络或格式异常都降级为「无补全」，不影响确定性解析结果。
"""

from __future__ import annotations

import json
import logging
import re

import httpx

from .base import (
    FieldContext,
    FieldSemantics,
    ResourceContext,
    ResourceSemantics,
    SemanticProvider,
)

logger = logging.getLogger(__name__)

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)

FIELD_SYSTEM_PROMPT = """你是数据资产语义分析助手。根据给定的字段元数据与数据画像，判断字段的业务含义。

只输出一个 JSON 对象，不要输出任何解释或 Markdown 代码块，格式严格如下：
{
  "display_name": "中文业务名称，不超过 12 字",
  "description": "一句话业务含义说明",
  "business_type": "英文下划线风格的类型标识，如 customer_identifier / monetary_amount",
  "tags": ["标签1", "标签2"],
  "unit": "计量单位，没有则填 null",
  "confidence": 0.0 到 1.0 之间的置信度
}

判断要求：
1. 字段名可能是缩写或同义词（cust_no、amt、qty），需结合 SQL 表达式、来源表、物理类型、脱敏样例和统计特征推断；
2. 依据不足时降低 confidence，不要编造业务含义；
3. 不要输出输入中不存在的字段。"""

RESOURCE_SYSTEM_PROMPT = """你是数据资产语义分析助手。根据资源编码、字段清单、来源表和行数，为该数据资源起名并打标签。

只输出一个 JSON 对象，不要输出任何解释或 Markdown 代码块，格式严格如下：
{
  "name": "中文资源名称，不超过 20 字",
  "description": "一句话资源用途说明",
  "tags": ["标签1", "标签2"],
  "confidence": 0.0 到 1.0 之间的置信度
}

若已提供人工填写的 resource_description，则 name 与 description 需与其保持一致，不要另起炉灶。"""


class OpenAICompatibleProvider(SemanticProvider):
    """调用兼容 OpenAI 协议的小模型 API。"""

    name = "openai_compatible"

    def __init__(
        self,
        api_base: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
        max_retries: int = 1,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self.endpoint = _chat_completions_url(api_base)
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            transport=transport,  # 仅测试注入，生产为 None 走默认传输
        )
        self._max_retries = max_retries
        # 同一批次内相同字段只问一次，控制调用量。
        self._field_cache: dict[str, FieldSemantics] = {}
        self._resource_cache: dict[str, ResourceSemantics] = {}

    # ---- 对外接口 ----

    def analyze_field(self, context: FieldContext) -> FieldSemantics:
        cache_key = json.dumps(context.to_payload(), sort_keys=True, ensure_ascii=False)
        if cache_key in self._field_cache:
            return self._field_cache[cache_key]

        payload = self._complete(FIELD_SYSTEM_PROMPT, context.to_payload(), purpose="字段语义")
        result = self._parse_field_payload(payload)
        self._field_cache[cache_key] = result
        return result

    def analyze_resource(self, context: ResourceContext) -> ResourceSemantics:
        cache_key = json.dumps(context.to_payload(), sort_keys=True, ensure_ascii=False)
        if cache_key in self._resource_cache:
            return self._resource_cache[cache_key]

        payload = self._complete(RESOURCE_SYSTEM_PROMPT, context.to_payload(), purpose="资源语义")
        result = self._parse_resource_payload(payload)
        self._resource_cache[cache_key] = result
        return result

    def close(self) -> None:
        self._client.close()

    # ---- HTTP ----

    def _complete(self, system_prompt: str, user_payload: dict, purpose: str) -> dict | None:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }

        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.post(self.endpoint, json=body)
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                return _loads_json_object(content)
            except httpx.HTTPStatusError as exc:
                # 4xx 多为请求本身不合法（如模型不支持 JSON 模式），重试无意义。
                if exc.response.status_code < 500 or attempt == self._max_retries:
                    logger.warning("小模型 %s 调用失败（HTTP %s），降级为无补全。",
                                   purpose, exc.response.status_code)
                    return None
            except Exception as exc:  # noqa: BLE001 - 外部服务不可控，一律降级
                if attempt == self._max_retries:
                    logger.warning("小模型 %s 调用失败（%s），降级为无补全。", purpose, exc)
                    return None
        return None

    # ---- 解析 ----

    def _parse_field_payload(self, payload: dict | None) -> FieldSemantics:
        if not payload:
            return FieldSemantics(confidence=0.0, provider=self.name)
        return FieldSemantics(
            display_name=_clean_str(payload.get("display_name")),
            description=_clean_str(payload.get("description")),
            business_type=_clean_str(payload.get("business_type")),
            tags=_clean_tags(payload.get("tags")),
            unit=_clean_str(payload.get("unit")),
            confidence=_clean_confidence(payload.get("confidence")),
            provider=self.name,
        )

    def _parse_resource_payload(self, payload: dict | None) -> ResourceSemantics:
        if not payload:
            return ResourceSemantics(confidence=0.0, provider=self.name)
        return ResourceSemantics(
            name=_clean_str(payload.get("name")),
            description=_clean_str(payload.get("description")),
            tags=_clean_tags(payload.get("tags")),
            confidence=_clean_confidence(payload.get("confidence")),
            provider=self.name,
        )


# ---------------------------------------------------------------- 工具


def _chat_completions_url(api_base: str) -> str:
    base = (api_base or "").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _loads_json_object(content: str) -> dict | None:
    """解析模型返回内容。容忍 Markdown 代码块包裹。"""
    if not isinstance(content, str):
        return None
    text = _FENCE.sub("", content.strip())
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # 模型偶尔会在 JSON 前后带说明文字，退而求其次截取最外层花括号。
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _clean_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    # 模型常用 "null"/"N/A" 之类的字符串表示缺失。
    if not text or text.lower() in {"null", "none", "n/a", "na", "未知", "无"}:
        return None
    return text


def _clean_tags(value: object) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    tags: list[str] = []
    for item in value:
        text = _clean_str(item)
        if text and text not in tags:
            tags.append(text)
    return tags


def _clean_confidence(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, round(number, 4)))
