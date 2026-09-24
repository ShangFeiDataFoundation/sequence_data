"""按主文件名自动配对 SQL 与数据文件（方案 §3.1）。

customer_profile.sql 自动关联 customer_profile.csv / .parquet / .xlsx / .jsonl。
同名但多种格式都存在时，按下方优先级取一个，保证结果确定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# 同名多格式时的取舍顺序：Parquet 类型最明确，优先使用。
EXTENSION_PRIORITY = (".parquet", ".pq", ".csv", ".tsv", ".txt", ".jsonl", ".ndjson", ".json", ".xlsx", ".xls")

SQL_SUFFIXES = {".sql"}


@dataclass
class PairingResult:
    """配对结果。"""

    # resource_code -> 数据文件名
    pairs: dict[str, str] = field(default_factory=dict)
    # resource_code -> 数据文件名，仅自动配对成功的
    auto_matched: set[str] = field(default_factory=set)
    unmatched_data_files: list[str] = field(default_factory=list)

    def data_file_for(self, resource_code: str) -> str | None:
        return self.pairs.get(resource_code)


def is_sql_file(name: str) -> bool:
    return Path(name).suffix.lower() in SQL_SUFFIXES


def resource_code_of(filename: str) -> str:
    """资源编码取 SQL 文件主名，与方案示例 customer_profile.sql -> customer_profile 一致。"""
    return Path(filename).stem


def _priority(filename: str) -> int:
    suffix = Path(filename).suffix.lower()
    return EXTENSION_PRIORITY.index(suffix) if suffix in EXTENSION_PRIORITY else len(EXTENSION_PRIORITY)


def pair_files(
    sql_filenames: list[str],
    data_filenames: list[str],
    mappings: dict[str, str] | None = None,
) -> PairingResult:
    """按主文件名配对，并应用用户手动指定的映射。

    mappings 的键可以是 SQL 文件名，也可以是资源编码；值为数据文件名，
    值为空串表示解除该资源的配对。
    """
    mappings = mappings or {}
    result = PairingResult()

    # 数据文件按主名归组，同名多格式时取优先级最高者。
    by_stem: dict[str, list[str]] = {}
    for name in data_filenames:
        by_stem.setdefault(Path(name).stem.lower(), []).append(name)

    consumed: set[str] = set()
    for sql_name in sql_filenames:
        code = resource_code_of(sql_name)
        # 手动映射优先：先按 SQL 文件名匹配，再按资源编码匹配。
        manual = mappings.get(sql_name, mappings.get(code, None))
        if manual is not None:
            if manual == "":
                continue  # 显式解除配对
            result.pairs[code] = manual
            consumed.add(manual)
            continue

        candidates = sorted(by_stem.get(code.lower(), []), key=_priority)
        if candidates:
            chosen = next((c for c in candidates if c not in consumed), candidates[0])
            result.pairs[code] = chosen
            result.auto_matched.add(code)
            consumed.add(chosen)

    result.unmatched_data_files = [name for name in data_filenames if name not in consumed]
    return result
