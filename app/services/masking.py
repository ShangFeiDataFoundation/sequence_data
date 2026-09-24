"""样例脱敏（方案 §4.2）。

解析结果中的 examples 和小模型入参都必须先经过这里。
原则：保留「形状」信号（字符类别、长度量级、小数位），去掉可识别到具体个体的内容。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

import pandas as pd

# 字符串样例最多保留的字符数，避免超长文本进入结果。
MAX_SAMPLE_LEN = 64

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_CN = re.compile(r"^1[3-9]\d{9}$")
_DIGITS = re.compile(r"^\d+$")
_INT_LIKE = re.compile(r"^[+-]?\d+$")
_FLOAT_LIKE = re.compile(r"^[+-]?\d*\.\d+$")

# 格式模式识别用的字符类别。
_CHAR_CLASS = [
    (re.compile(r"[A-Z]"), "A"),
    (re.compile(r"[a-z]"), "a"),
    (re.compile(r"\d"), "9"),
]


# 短字符串的保留上限：4 字以内只留首字符，否则「首 1 + 末 2」会暴露大半内容
# （如三字姓名「吕桂英」会变成「吕***桂英」）。
_SHORT_STRING_LEN = 4


def _mask_string(text: str) -> str:
    """字符串掩码：长串保留首字符与末两位，短串只保留首字符。

    长串对应方案示例：CUST0001 -> C***01
    """
    if len(text) <= _SHORT_STRING_LEN:
        return f"{text[:1]}***"
    return f"{text[0]}***{text[-2:]}"


def _mask_digits(digits: str) -> str:
    """纯数字串按长度伸缩掩码，手机号保留号段。"""
    if _PHONE_CN.match(digits):
        return f"{digits[:3]}****{digits[-4:]}"
    if len(digits) <= 4:
        return "*" * len(digits)
    return f"{digits[:2]}{'*' * (len(digits) - 4)}{digits[-2:]}"


def _mask_numeric_shape(text: str) -> str:
    """数值样例只保留符号与小数位形状，如 1234.56 -> ****.**"""
    sign = text[0] if text[0] in "+-" else ""
    body = text[len(sign):]
    masked = "".join("*" if ch.isdigit() else ch for ch in body)
    return f"{sign}{masked}"


def mask_value(value: Any) -> str | None:
    """把单个原始值转为脱敏样例。空值返回 None。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, float) and pd.isna(value):
        return None

    if isinstance(value, bool):
        return str(value)

    if isinstance(value, (int,)) and not isinstance(value, bool):
        return _mask_digits(str(abs(value)))

    if isinstance(value, float):
        return _mask_numeric_shape(f"{value:.4f}".rstrip("0").rstrip("."))

    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "<na>"}:
        return None

    if len(text) > MAX_SAMPLE_LEN:
        text = text[:MAX_SAMPLE_LEN]

    if _EMAIL.match(text):
        local, _, domain = text.partition("@")
        return f"{local[:1]}***@{domain}"

    if _DIGITS.match(text):
        return _mask_digits(text)

    if _INT_LIKE.match(text) or _FLOAT_LIKE.match(text):
        return _mask_numeric_shape(text)

    # 日期时间原样保留：它在 statistics 里本就以 min/max 暴露，且是判断
    # 字段业务含义的重要信号，单独打码反而会削弱语义判断。
    if _looks_like_datetime(text):
        return text

    return _mask_string(text)


def _looks_like_datetime(text: str) -> bool:
    if len(text) < 8 or len(text) > 40:
        return False
    return bool(
        re.match(r"^\d{4}[-/]\d{1,2}([-/]\d{1,2})?", text)
        or re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:", text)
    )


def mask_examples(values: Iterable[Any], limit: int) -> list[str]:
    """取去重后的脱敏样例，最多 limit 个。"""
    examples: list[str] = []
    seen: set[str] = set()
    for value in values:
        masked = mask_value(value)
        if masked is None or masked in seen:
            continue
        seen.add(masked)
        examples.append(masked)
        if len(examples) >= limit:
            break
    return examples


def char_class(char: str) -> str:
    for pattern, label in _CHAR_CLASS:
        if pattern.match(char):
            return label
    return char  # 分隔符等原样保留


def format_pattern(text: str, max_len: int = 24) -> str | None:
    """把样例转成格式模式，如 CUST-0001 -> A-9；连续同类字符折叠为一个符号。"""
    if not text:
        return None
    classes = [char_class(ch) for ch in text[:max_len]]
    collapsed: list[str] = []
    for cls in classes:
        if collapsed and collapsed[-1] == cls and cls in {"A", "a", "9"}:
            continue
        collapsed.append(cls)
    pattern = "".join(collapsed)
    return pattern if pattern and pattern != text else None


def dominant_format_pattern(values: Iterable[Any], limit: int = 200) -> str | None:
    """在样例中取出现次数最多的格式模式，作为标识字段的格式特征。"""
    counts: dict[str, int] = {}
    checked = 0
    for value in values:
        if checked >= limit:
            break
        if value is None or (isinstance(value, float) and pd.isna(value)):
            continue
        checked += 1
        pattern = format_pattern(str(value).strip())
        if pattern:
            counts[pattern] = counts.get(pattern, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]
