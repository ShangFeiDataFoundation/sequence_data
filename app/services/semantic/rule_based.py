"""基于字段名、词典、类型与 SQL 表达式的规则实现（方案 §4.4）。

用于本地开发，以及小模型 API 不可用时的降级路径。规则命中的置信度按
「整体名精确匹配 > 词元匹配 > 子串匹配」递减，多个规则并列时输出候选而不武断取一个。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .base import (
    FieldContext,
    FieldSemantics,
    ResourceContext,
    ResourceSemantics,
    SemanticProvider,
)

# 命中强度：决定 confidence 与是否需要人工确认。
_SCORE_EXACT = 1.0
_SCORE_TOKEN = 0.88
_SCORE_SUBSTRING = 0.72
# 首位与次位分差低于该值时视为存在多个候选。
_AMBIGUITY_MARGIN = 0.08


@dataclass(frozen=True)
class Rule:
    """一条语义规则。"""

    tokens: tuple[str, ...]
    business_type: str
    display_name: str
    tags: tuple[str, ...]
    unit: str | None = None
    # 期望的物理类型；命中可略微提分，用于在近义规则间取舍。
    type_affinity: tuple[str, ...] = ()


RULES: tuple[Rule, ...] = (
    # 业务编码与内部主键分开命名：两者常同时出现在一张表里，混用会难以区分。
    Rule(
        tokens=("cust_no", "cust_nbr", "customer_no", "client_no", "member_no", "user_no"),
        business_type="customer_code", display_name="客户编号",
        tags=("客户", "标识"), type_affinity=("string",),
    ),
    Rule(
        tokens=("cust_id", "customer_id", "client_id", "member_id", "user_id", "uid"),
        business_type="customer_identifier", display_name="客户标识",
        tags=("客户", "标识"), type_affinity=("string", "integer"),
    ),
    Rule(
        tokens=("order_id", "order_no", "order_code", "trade_no", "txn_id", "transaction_id"),
        business_type="order_identifier", display_name="订单编号", tags=("交易", "标识"),
    ),
    Rule(
        tokens=("id", "no", "code", "key", "uuid", "guid", "sn"),
        business_type="identifier", display_name="标识", tags=("标识",),
    ),
    Rule(
        tokens=("amt", "amount", "money", "fee", "price", "cost", "payment", "pay",
                "balance", "total_amount", "revenue", "gmv"),
        business_type="monetary_amount", display_name="金额", tags=("金额",),
        unit="元", type_affinity=("number", "integer"),
    ),
    Rule(
        tokens=("qty", "quantity", "cnt", "count", "num", "number", "total_num", "volume"),
        business_type="quantity", display_name="数量", tags=("数量",),
        type_affinity=("integer", "number"),
    ),
    Rule(
        tokens=("dt", "ds", "bizdate", "stat_date", "date", "day", "partition"),
        business_type="date", display_name="日期", tags=("时间",), type_affinity=("datetime",),
    ),
    Rule(
        tokens=("ts", "time", "timestamp", "datetime", "created_at", "updated_at",
                "create_time", "update_time", "last_order_at", "start_time", "end_time"),
        business_type="timestamp", display_name="时间", tags=("时间",), type_affinity=("datetime",),
    ),
    Rule(
        tokens=("name", "nm", "title", "full_name"),
        business_type="name", display_name="名称", tags=("名称",), type_affinity=("string",),
    ),
    Rule(
        tokens=("status", "state", "flag", "stage", "phase"),
        business_type="status", display_name="状态", tags=("状态",),
    ),
    Rule(
        tokens=("rate", "ratio", "pct", "percent", "proportion"),
        business_type="ratio", display_name="比率", tags=("比率",), type_affinity=("number",),
    ),
    Rule(
        tokens=("addr", "address", "location", "region_name"),
        business_type="address", display_name="地址", tags=("地址",), type_affinity=("string",),
    ),
    Rule(
        tokens=("phone", "mobile", "tel", "telephone", "contact"),
        business_type="phone_number", display_name="电话", tags=("联系方式",), type_affinity=("string",),
    ),
    Rule(
        tokens=("email", "mail", "email_addr"),
        business_type="email", display_name="邮箱", tags=("联系方式",), type_affinity=("string",),
    ),
    Rule(
        tokens=("gender", "sex"),
        business_type="gender", display_name="性别", tags=("人口属性",), type_affinity=("string",),
    ),
    Rule(
        tokens=("age",),
        business_type="age", display_name="年龄", tags=("人口属性",), type_affinity=("integer",),
    ),
    Rule(
        tokens=("city", "province", "country", "area", "district", "county", "region"),
        business_type="region", display_name="地区", tags=("地区",), type_affinity=("string",),
    ),
    Rule(
        tokens=("dept", "department", "org", "organization", "team"),
        business_type="department", display_name="部门", tags=("组织",), type_affinity=("string",),
    ),
    Rule(
        tokens=("product", "prod", "sku", "item", "goods", "material"),
        business_type="product_reference", display_name="产品", tags=("产品",),
    ),
    Rule(
        tokens=("channel", "source", "medium", "platform"),
        business_type="channel", display_name="渠道", tags=("渠道",), type_affinity=("string",),
    ),
    Rule(
        tokens=("desc", "description", "remark", "comment", "note", "memo"),
        business_type="description", display_name="描述", tags=("描述",), type_affinity=("string",),
    ),
    Rule(
        tokens=("level", "tier", "grade", "rank", "class"),
        business_type="level", display_name="等级", tags=("分级",),
    ),
)

# 中文词典：字段名可能直接用中文命名。
CJK_LEXICON: tuple[tuple[str, str, str, tuple[str, ...], str | None], ...] = (
    ("客户编号", "customer_identifier", "客户编号", ("客户", "标识"), None),
    ("客户", "customer_reference", "客户", ("客户",), None),
    ("订单", "order_reference", "订单", ("交易",), None),
    ("金额", "monetary_amount", "金额", ("金额",), "元"),
    ("数量", "quantity", "数量", ("数量",), None),
    ("日期", "date", "日期", ("时间",), None),
    ("时间", "timestamp", "时间", ("时间",), None),
    ("名称", "name", "名称", ("名称",), None),
    ("姓名", "name", "姓名", ("名称",), None),
    ("状态", "status", "状态", ("状态",), None),
    ("地址", "address", "地址", ("地址",), None),
    ("电话", "phone_number", "电话", ("联系方式",), None),
    ("邮箱", "email", "邮箱", ("联系方式",), None),
    ("性别", "gender", "性别", ("人口属性",), None),
    ("年龄", "age", "年龄", ("人口属性",), None),
    ("城市", "region", "城市", ("地区",), None),
    ("省份", "region", "省份", ("地区",), None),
    ("部门", "department", "部门", ("组织",), None),
    ("产品", "product_reference", "产品", ("产品",), None),
    ("渠道", "channel", "渠道", ("渠道",), None),
    ("比率", "ratio", "比率", ("比率",), None),
    ("占比", "ratio", "占比", ("比率",), None),
    ("等级", "level", "等级", ("分级",), None),
)

_TOKEN_SPLIT = re.compile(r"[^0-9A-Za-z一-鿿]+")


class RuleBasedProvider(SemanticProvider):
    """规则提供方。完全离线、确定性，同一输入必得同一输出。"""

    name = "rule_based"

    def analyze_field(self, context: FieldContext) -> FieldSemantics:
        name = context.field_name or ""
        lowered = name.lower()
        tokens = [t for t in _TOKEN_SPLIT.split(lowered) if t]

        matches = self._collect_matches(lowered, tokens, context.physical_type)
        if not matches:
            return FieldSemantics(
                confidence=0.0,
                provider=self.name,
                tags=self._tags_from_type(context.physical_type),
            )

        matches.sort(key=lambda m: m[0], reverse=True)
        best_score, best_rule = matches[0]

        # 次优规则分数接近时视为存在多个候选，交由人工确认。
        rivals = [r for score, r in matches[1:] if best_score - score < _AMBIGUITY_MARGIN]
        if rivals:
            candidates = [best_rule.display_name] + [r.display_name for r in rivals[:2]]
            return FieldSemantics(
                # 候选并列时不给出名称与描述，避免误导下游展示。
                business_type=None,
                tags=self._merge_tags(best_rule.tags, context.physical_type),
                confidence=round(best_score * 0.5, 4),
                candidates=_dedupe(candidates),
                provider=self.name,
            )

        return FieldSemantics(
            display_name=best_rule.display_name,
            business_type=best_rule.business_type,
            tags=self._merge_tags(best_rule.tags, context.physical_type),
            unit=best_rule.unit,
            confidence=round(best_score, 4),
            provider=self.name,
        )

    def analyze_resource(self, context: ResourceContext) -> ResourceSemantics:
        # 资源级名称由 resource_code 还原，标签取字段标签的高频项。
        tags: list[str] = []
        if context.has_aggregation:
            tags.append("汇总")
        if len(context.source_tables) > 1:
            tags.append("多表关联")
        return ResourceSemantics(
            name=_prettify_code(context.resource_code),
            tags=tags,
            confidence=0.5,
            provider=self.name,
        )

    # ---- 内部 ----

    def _collect_matches(
        self, lowered: str, tokens: list[str], physical_type: str | None
    ) -> list[tuple[float, Rule]]:
        matches: list[tuple[float, Rule]] = []

        for rule in RULES:
            score = self._score_rule(rule, lowered, tokens)
            if score is None:
                continue
            if physical_type and physical_type in rule.type_affinity:
                score = min(1.0, score + 0.04)
            matches.append((score, rule))

        # 中文命中共存时一并纳入比较。
        for word, business_type, display_name, tags, unit in CJK_LEXICON:
            if word in lowered:
                score = _SCORE_EXACT if lowered == word else _SCORE_TOKEN
                matches.append(
                    (
                        score,
                        Rule(tokens=(word,), business_type=business_type,
                             display_name=display_name, tags=tags, unit=unit),
                    )
                )
        return matches

    @staticmethod
    def _score_rule(rule: Rule, lowered: str, tokens: list[str]) -> float | None:
        if lowered in rule.tokens:
            return _SCORE_EXACT
        if any(token in rule.tokens for token in tokens):
            return _SCORE_TOKEN
        if any(token and token in lowered for token in rule.tokens):
            return _SCORE_SUBSTRING
        return None

    @staticmethod
    def _merge_tags(rule_tags: tuple[str, ...], physical_type: str | None) -> list[str]:
        return _dedupe(list(rule_tags) + RuleBasedProvider._tags_from_type(physical_type))

    @staticmethod
    def _tags_from_type(physical_type: str | None) -> list[str]:
        mapping = {
            "datetime": ["时间"],
            "number": ["数值"],
            "integer": ["数值"],
            "boolean": ["布尔"],
            "string": ["文本"],
        }
        return mapping.get(physical_type or "", [])


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _prettify_code(code: str) -> str:
    """把 customer_profile 还原成 Customer Profile 供展示，中文原名直接返回。"""
    if re.search(r"[一-鿿]", code):
        return code
    parts = [p for p in _TOKEN_SPLIT.split(code) if p]
    return " ".join(p.capitalize() for p in parts) if parts else code


def aggregate_resource_tags(field_tag_lists: Iterable[Sequence[str]], top_n: int = 8) -> list[str]:
    """资源标签：取各字段标签中除通用类型标签外的高频项。"""
    generic = {"文本", "数值", "时间", "布尔"}
    counter: Counter[str] = Counter()
    for tags in field_tag_lists:
        for tag in tags:
            if tag not in generic:
                counter[tag] += 1
    return [tag for tag, _ in counter.most_common(top_n)]
