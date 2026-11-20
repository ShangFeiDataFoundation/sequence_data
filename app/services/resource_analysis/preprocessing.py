"""通用时序资源长表/宽表规范化。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ...config import Settings


class ResourceInputError(ValueError):
    """无法构成有效时序资源。"""


@dataclass
class PreparedResource:
    frame: pd.DataFrame
    time_field: str
    entity_fields: list[str]
    layout: str
    metric_count: int


def _detect_time_field(frame: pd.DataFrame, preferred: str | None) -> str:
    if preferred:
        if preferred not in frame.columns:
            raise ResourceInputError(f"时间字段不存在: {preferred}")
        return preferred
    candidates: list[tuple[float, str]] = []
    for column in frame.columns:
        values = frame[column].dropna()
        if values.empty:
            continue
        converted = pd.to_datetime(
            values.astype(str),
            errors="coerce",
            format="mixed",
            utc=True,
        )
        rate = float(converted.notna().mean())
        hint = 0.08 if any(
            token in str(column).lower()
            for token in ("time", "date", "dt", "ts", "timestamp", "时间", "日期")
        ) else 0.0
        candidates.append((rate + hint, str(column)))
    if not candidates or max(candidates)[0] < 0.8:
        raise ResourceInputError("无法自动识别时间字段。")
    return max(candidates)[1]


def _detect_entity_fields(
    frame: pd.DataFrame,
    preferred: list[str] | None,
    excluded: set[str],
) -> list[str]:
    if preferred:
        missing = [name for name in preferred if name not in frame.columns]
        if missing:
            raise ResourceInputError(f"实体字段不存在: {', '.join(missing)}")
        return list(dict.fromkeys(preferred))
    ordered: list[str] = []
    for name in frame.columns:
        lowered = str(name).lower()
        if str(name) in excluded:
            continue
        if lowered in {
            "aircraft_id",
            "sensor_id",
            "device_id",
            "entity_id",
            "customer_id",
        } or any(token in lowered for token in ("飞机编号", "传感器编号", "设备编号")):
            ordered.append(str(name))
    return ordered[:3]


def _find_column(frame: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    lowered = {str(column).lower(): str(column) for column in frame.columns}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    return None


def prepare_resource(
    frame: pd.DataFrame,
    *,
    time_field: str | None,
    entity_fields: list[str] | None,
    parameter_field: str | None,
    value_field: str | None,
    settings: Settings,
) -> PreparedResource:
    if frame.empty:
        raise ResourceInputError("数据文件没有可分析的数据行。")
    if len(frame) > settings.timeseries_max_points:
        raise ResourceInputError(
            f"时序数据超过最大分析行数 {settings.timeseries_max_points}。"
        )
    time_column = _detect_time_field(frame, time_field)
    parameter_column = parameter_field or _find_column(
        frame,
        ("parameter_id", "parameter", "attribute_id", "metric_id", "参数"),
    )
    value_column = value_field or _find_column(
        frame,
        ("value", "reading", "measurement", "参数值", "数值"),
    )
    excluded = {time_column, parameter_column, value_column}
    entities = _detect_entity_fields(frame, entity_fields, {x for x in excluded if x})

    metadata = {
        time_column,
        *entities,
        "flight_phase",
        "phase",
        "flight_id",
        "is_fault",
        "is_anomaly",
        "飞行阶段",
    }
    if parameter_column and value_column:
        normalized = pd.DataFrame(
            {
                "__time__": frame[time_column],
                "__metric__": frame[parameter_column].astype(str),
                "value": frame[value_column],
                **{field: frame[field].astype(str) for field in entities},
            }
        )
        layout = "long"
    else:
        numeric_columns = [
            str(column)
            for column in frame.columns
            if str(column) not in metadata
            and pd.to_numeric(frame[column], errors="coerce").notna().mean() >= 0.7
        ]
        if not numeric_columns:
            raise ResourceInputError("没有识别到可分析的数值指标列。")
        id_columns = [time_column, *entities]
        normalized = frame[id_columns + numeric_columns].melt(
            id_vars=id_columns,
            value_vars=numeric_columns,
            var_name="__metric__",
            value_name="value",
        ).rename(columns={time_column: "__time__"})
        layout = "wide"

    parsed_time = pd.to_datetime(
        normalized["__time__"].astype(str),
        errors="coerce",
        format="mixed",
        utc=True,
    )
    valid = parsed_time.notna()
    normalized = normalized.loc[valid].copy()
    normalized["__time__"] = parsed_time.loc[valid]
    normalized["value"] = pd.to_numeric(normalized["value"], errors="coerce")
    normalized = normalized.dropna(subset=["__metric__", "value"])
    if normalized.empty or not np.isfinite(normalized["value"]).any():
        raise ResourceInputError("没有有效数值。")
    sort_fields = [*entities, "__metric__", "__time__"]
    normalized = normalized.sort_values(sort_fields, kind="stable").reset_index(drop=True)
    return PreparedResource(
        frame=normalized,
        time_field=time_column,
        entity_fields=entities,
        layout=layout,
        metric_count=int(normalized["__metric__"].nunique()),
    )
