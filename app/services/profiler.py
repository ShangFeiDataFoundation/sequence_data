"""数据画像层：从真实数据中提取物理类型、分布、枚举与标识字段（方案 §4.2）。

这一层同样不调用模型，输出全部由确定性计算得到。
统计最多读取 profile_max_rows 行；行数总量单独统计，不受采样上限影响。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from ..config import Settings, get_settings
from ..schemas import FieldStatistics
from .masking import dominant_format_pattern, mask_examples

# 低基数字段才输出高频值与枚举候选：它们本质是类别，不是个体。
MAX_ENUM_DISTINCT = 50
TOP_VALUE_LIMIT = 10

CSV_SUFFIXES = {".csv", ".txt", ".tsv"}
PARQUET_SUFFIXES = {".parquet", ".pq"}
EXCEL_SUFFIXES = {".xlsx", ".xls"}
JSONL_SUFFIXES = {".jsonl", ".ndjson", ".json"}

SUPPORTED_SUFFIXES = CSV_SUFFIXES | PARQUET_SUFFIXES | EXCEL_SUFFIXES | JSONL_SUFFIXES


class ProfileError(RuntimeError):
    """数据文件无法读取或格式不受支持。"""


@dataclass
class ProfileResult:
    """一份数据文件的完整画像。"""

    row_count: int = 0
    sampled_row_count: int = 0
    column_count: int = 0
    columns: list[str] = field(default_factory=list)
    statistics: dict[str, FieldStatistics] = field(default_factory=dict)
    examples: dict[str, list[str]] = field(default_factory=dict)
    sample_size_warning: bool = False
    error: str | None = None


# ---------------------------------------------------------------- 读取


def is_supported(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_SUFFIXES


def load_dataframe(path: str | Path, settings: Settings | None = None) -> pd.DataFrame:
    """读取数据文件，最多读取 profile_max_rows 行用于统计。"""
    settings = settings or get_settings()
    path = Path(path)
    suffix = path.suffix.lower()
    limit = settings.profile_max_rows

    try:
        if suffix in CSV_SUFFIXES:
            separator = "\t" if suffix == ".tsv" else ","
            return pd.read_csv(
                path, nrows=limit, sep=separator, dtype=str,
                keep_default_na=True, encoding="utf-8-sig", on_bad_lines="warn",
            )
        if suffix in PARQUET_SUFFIXES:
            return pd.read_parquet(path).head(limit)
        if suffix in EXCEL_SUFFIXES:
            return pd.read_excel(path, nrows=limit, dtype=str)
        if suffix in JSONL_SUFFIXES:
            return _read_jsonl(path, limit)
    except Exception as exc:  # noqa: BLE001 - 统一收敛为 ProfileError 交给上层标记失败
        raise ProfileError(f"读取数据文件失败: {exc}") from exc

    raise ProfileError(
        f"不支持的数据文件格式 {suffix}，支持 CSV、Parquet、XLSX、JSON Lines。"
    )


def _read_jsonl(path: Path, limit: int) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue  # 跳过损坏行，不因单行中断整份画像
            records.append(obj if isinstance(obj, dict) else {"value": obj})
            if len(records) >= limit:
                break
    return pd.json_normalize(records) if records else pd.DataFrame()


def count_total_rows(path: str | Path, sample: pd.DataFrame, settings: Settings | None = None) -> int:
    """统计文件真实总行数。

    采样读取时只拿到前 N 行，总量需按格式单独统计：
    Parquet 读元数据，CSV/JSONL 数行，Excel 只能以实际读到的行为准。
    """
    settings = settings or get_settings()
    path = Path(path)
    suffix = path.suffix.lower()

    try:
        if suffix in CSV_SUFFIXES:
            with path.open("rb") as fh:
                return max(sum(1 for _ in fh) - 1, 0)  # 扣除表头
        if suffix in PARQUET_SUFFIXES:
            import pyarrow.parquet as pq

            return pq.ParquetFile(path).metadata.num_rows
        if suffix in JSONL_SUFFIXES:
            with path.open("rb") as fh:
                return sum(1 for line in fh if line.strip())
    except Exception:  # noqa: BLE001 - 统计失败时退回采样行数，不阻断解析
        return len(sample)
    return len(sample)


# ---------------------------------------------------------------- 画像


def profile_file(path: str | Path, settings: Settings | None = None) -> ProfileResult:
    """对一份数据文件做完整画像。"""
    settings = settings or get_settings()
    path = Path(path)

    if not path.exists():
        return ProfileResult(error=f"数据文件不存在: {path.name}")

    try:
        frame = load_dataframe(path, settings)
    except ProfileError as exc:
        return ProfileResult(error=str(exc))

    if frame.empty:
        return ProfileResult(
            column_count=len(frame.columns),
            columns=[str(c) for c in frame.columns],
            error="数据文件没有可读入的数据行。",
        )

    total_rows = count_total_rows(path, frame, settings)
    statistics: dict[str, FieldStatistics] = {}
    examples: dict[str, list[str]] = {}

    for column in frame.columns:
        name = str(column)
        series = frame[column]
        statistics[name] = _profile_series(series, settings)
        examples[name] = mask_examples(series.dropna().tolist(), settings.profile_max_examples)

    return ProfileResult(
        row_count=total_rows,
        sampled_row_count=len(frame),
        column_count=len(frame.columns),
        columns=[str(c) for c in frame.columns],
        statistics=statistics,
        examples=examples,
        sample_size_warning=total_rows < settings.sample_size_warning_threshold,
    )


def _profile_series(series: pd.Series, settings: Settings) -> FieldStatistics:
    """对单列做画像。"""
    row_count = int(len(series))
    if row_count == 0:
        return FieldStatistics(physical_type="unknown", row_count=0)

    null_count = int(series.isna().sum())
    non_null = series.dropna()
    null_rate = _rate(null_count, row_count)

    if non_null.empty:
        return FieldStatistics(
            physical_type="unknown", row_count=row_count,
            null_count=null_count, null_rate=1.0,
        )

    distinct_count = int(non_null.nunique())
    distinct_rate = _rate(distinct_count, int(len(non_null)))
    physical_type, numeric, temporal = _resolve_type(non_null)

    stats = FieldStatistics(
        physical_type=physical_type,
        row_count=row_count,
        null_count=null_count,
        null_rate=null_rate,
        distinct_count=distinct_count,
        distinct_rate=distinct_rate,
    )

    if numeric is not None:
        _fill_numeric(stats, numeric)
    elif temporal is not None:
        _fill_temporal(stats, temporal)
    else:
        _fill_text(stats, non_null)

    _fill_identifier(stats, non_null, distinct_rate, null_rate, physical_type)
    return stats


def _resolve_type(series: pd.Series) -> tuple[str, pd.Series | None, pd.Series | None]:
    """判定物理类型，并返回可用于统计的数值/时间序列。"""
    if pd.api.types.is_bool_dtype(series):
        return "boolean", None, None
    if pd.api.types.is_integer_dtype(series):
        return "integer", series.astype("float64"), None
    if pd.api.types.is_float_dtype(series):
        return "number", series.astype("float64"), None
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime", None, series

    # 字符串列：先试时间，再试数值，都失败才算文本。
    as_text = series.astype(str).str.strip()
    temporal = pd.to_datetime(as_text, errors="coerce", format="mixed")
    if temporal.notna().mean() >= 0.9:
        return "datetime", None, temporal

    numeric = pd.to_numeric(as_text, errors="coerce")
    if numeric.notna().mean() >= 0.9:
        integral = bool((numeric.dropna() % 1 == 0).all())
        return ("integer" if integral else "number"), numeric, None

    return "string", None, None


def _fill_numeric(stats: FieldStatistics, numeric: pd.Series) -> None:
    clean = numeric.dropna()
    if clean.empty:
        return
    stats.min = _round(clean.min())
    stats.max = _round(clean.max())
    stats.mean = _round(clean.mean())
    stats.median = _round(clean.median())
    stats.quantiles = {
        "p25": _round(clean.quantile(0.25)),
        "p50": _round(clean.median()),
        "p75": _round(clean.quantile(0.75)),
    }


def _fill_temporal(stats: FieldStatistics, temporal: pd.Series) -> None:
    clean = temporal.dropna()
    if clean.empty:
        return
    earliest, latest = clean.min(), clean.max()
    stats.datetime_min = earliest.isoformat()
    stats.datetime_max = latest.isoformat()
    stats.time_span_days = _round((latest - earliest).total_seconds() / 86400)


def _fill_text(stats: FieldStatistics, series: pd.Series) -> None:
    lengths = series.astype(str).str.len()
    stats.max_length = int(lengths.max())
    stats.avg_length = _round(lengths.mean())

    # 只有低基数字段才展开取值分布：高基数文本多为自由文本或标识，展开既不安全也无意义。
    if stats.distinct_count is not None and stats.distinct_count <= MAX_ENUM_DISTINCT:
        counts = series.astype(str).value_counts().head(TOP_VALUE_LIMIT)
        total = int(len(series))
        stats.top_values = [
            {"value": str(value), "count": int(count), "rate": _rate(int(count), total)}
            for value, count in counts.items()
        ]
        stats.enum_candidates = [str(v) for v in counts.index.tolist()]


def _fill_identifier(
    stats: FieldStatistics,
    series: pd.Series,
    distinct_rate: float | None,
    null_rate: float | None,
    physical_type: str,
) -> None:
    """标识字段判定：近乎唯一、近乎非空即视为主键候选。"""
    if distinct_rate is None or null_rate is None:
        return
    stats.duplicate_rate = _round(max(0.0, 1.0 - distinct_rate))
    stats.is_identifier_candidate = bool(
        distinct_rate >= 0.95 and null_rate <= 0.05 and len(series) > 1
    )
    if stats.is_identifier_candidate or physical_type in {"integer", "string"}:
        stats.format_pattern = dominant_format_pattern(series.tolist())


def _rate(numerator: int, denominator: int) -> float:
    return _round(numerator / denominator) if denominator else 0.0


def _round(value: Any, digits: int = 4) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None
