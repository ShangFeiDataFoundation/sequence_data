"""时序资源统计特征提取与固定 JSON 生成。"""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

from ...config import Settings, get_settings
from ...schemas import (
    ResourceChartPoint,
    ResourceChartSeries,
    ResourceMetricFeature,
    ResourceQuality,
    StatisticalFeatureSet,
    StatisticalResourceDescriptor,
    TimeSeriesEntity,
    TimeSeriesTimeAxis,
)
from ...storage import new_id, now
from .. import profiler
from ..timeseries.statistical import (
    analyze_seasonalities,
    analyze_trend,
    build_events,
    build_segments,
    detect_anomalies,
    detect_change_points,
    infer_granularity,
    metric_relationships,
    profile_series,
)
from .preprocessing import PreparedResource, prepare_resource


def analyze_resource_file(
    path: str | Path,
    *,
    source_name: str | None = None,
    time_field: str | None = None,
    entity_fields: list[str] | None = None,
    parameter_field: str | None = None,
    value_field: str | None = None,
    settings: Settings | None = None,
) -> StatisticalResourceDescriptor:
    settings = settings or get_settings()
    source = Path(path)
    if not profiler.is_supported(source):
        raise ValueError(f"不支持的数据文件格式: {source.suffix}")
    frame = profiler.load_dataframe(source, settings)
    prepared = prepare_resource(
        frame,
        time_field=time_field,
        entity_fields=entity_fields,
        parameter_field=parameter_field,
        value_field=value_field,
        settings=settings,
    )
    return analyze_prepared_resource(
        prepared,
        source_file=source_name or source.name,
        settings=settings,
    )


def analyze_prepared_resource(
    prepared: PreparedResource,
    *,
    source_file: str,
    settings: Settings,
) -> StatisticalResourceDescriptor:
    time_axis = _build_time_axis(prepared)
    features: list[ResourceMetricFeature] = []
    charts: list[ResourceChartSeries] = []
    correlations = []
    groups = _entity_groups(prepared)

    for entity_key, group in groups:
        entity = _build_entity(entity_key, group, prepared.entity_fields)
        aligned: dict[str, pd.Series] = {}
        for metric, metric_group in group.groupby("__metric__", sort=True):
            series = (
                metric_group.set_index("__time__")["value"]
                .sort_index()
                .groupby(level=0)
                .mean()
            )
            if series.empty:
                continue
            seasonalities = analyze_seasonalities(
                series,
                granularity_seconds=time_axis.granularity_seconds,
            )
            anomalies = detect_anomalies(series, settings=settings)
            change_points = detect_change_points(series)
            segments = build_segments(series)
            events = build_events(
                change_points=change_points,
                segments=segments,
                anomalies=anomalies,
            )
            features.append(
                ResourceMetricFeature(
                    entity=entity,
                    metric_id=str(metric),
                    feature_id=new_id("feat"),
                    statistics=_statistical_features(series),
                    trend=analyze_trend(series),
                    seasonalities=seasonalities,
                    change_points=change_points,
                    segments=segments,
                    events=events,
                    anomalies=anomalies,
                )
            )
            charts.append(
                _chart_series(
                    entity_key=entity_key,
                    metric=str(metric),
                    series=series,
                )
            )
            aligned[str(metric)] = series
        if len(aligned) >= 2:
            correlations.extend(
                metric_relationships(
                    pd.DataFrame(aligned).sort_index(),
                    entity_key=entity_key,
                    threshold=settings.timeseries_correlation_threshold,
                )
            )

    return StatisticalResourceDescriptor(
        resource_id=new_id("res"),
        resource_name=Path(source_file).stem,
        source_file=source_file,
        layout=prepared.layout,
        time_axis=time_axis,
        entity_fields=prepared.entity_fields,
        value_fields=sorted(prepared.frame["__metric__"].unique().tolist()),
        quality=_build_quality(prepared, time_axis, features),
        features=features,
        charts=charts,
        correlations=correlations,
        generated_at=now(),
    )


def _statistical_features(series: pd.Series) -> StatisticalFeatureSet:
    profile = profile_series(series)
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty:
        return StatisticalFeatureSet(
            count=profile.count,
            missing_count=profile.missing_count,
            missing_rate=profile.missing_rate,
        )
    q1 = float(clean.quantile(0.25))
    q3 = float(clean.quantile(0.75))
    median = float(clean.median())
    mad = float((clean - median).abs().median())
    mean = float(clean.mean())
    std = float(clean.std(ddof=0))
    value_range = float(clean.max() - clean.min())
    variance = float(clean.var(ddof=0))
    cv = abs(std / mean) if abs(mean) > 1e-12 else None
    lag1 = None
    if len(clean) >= 3 and float(clean.std(ddof=0)) > 1e-12:
        corr = float(clean.autocorr(lag=1))
        lag1 = corr if math.isfinite(corr) else None
    rolling_window = max(3, min(24, len(clean) // 10))
    volatility = clean.rolling(rolling_window, min_periods=2).std(ddof=0).dropna()
    return StatisticalFeatureSet(
        count=len(series),
        valid_count=int(clean.count()),
        missing_count=profile.missing_count,
        missing_rate=profile.missing_rate,
        mean=_finite(mean),
        median=_finite(median),
        std=_finite(std),
        variance=_finite(variance),
        minimum=_finite(float(clean.min())),
        maximum=_finite(float(clean.max())),
        value_range=_finite(value_range),
        q1=_finite(q1),
        q3=_finite(q3),
        iqr=_finite(q3 - q1),
        mad=_finite(mad),
        coefficient_of_variation=_finite(cv),
        skewness=_finite(float(clean.skew())),
        kurtosis=_finite(float(clean.kurt())),
        zero_rate=round(float((clean == 0).mean()), 6),
        unique_rate=round(float(clean.nunique() / len(clean)), 6),
        first_value=_finite(float(clean.iloc[0])),
        last_value=_finite(float(clean.iloc[-1])),
        absolute_change=profile.absolute_change,
        relative_change=profile.relative_change,
        autocorrelation_lag1=_finite(lag1),
        rolling_volatility_mean=(
            _finite(float(volatility.mean())) if not volatility.empty else None
        ),
        rolling_volatility_max=(
            _finite(float(volatility.max())) if not volatility.empty else None
        ),
    )


def _chart_series(
    *,
    entity_key: str,
    metric: str,
    series: pd.Series,
    max_points: int = 200,
) -> ResourceChartSeries:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    step = max(1, math.ceil(len(clean) / max_points))
    sampled = clean.iloc[::step]
    if len(clean) and (sampled.empty or sampled.index[-1] != clean.index[-1]):
        sampled = pd.concat([sampled, clean.iloc[[-1]]])
    return ResourceChartSeries(
        label=f"{entity_key} / {metric}",
        entity_ref=entity_key,
        metric_id=metric,
        points=[
            ResourceChartPoint(
                timestamp=pd.Timestamp(timestamp).to_pydatetime(),
                value=round(float(value), 6),
            )
            for timestamp, value in sampled.items()
        ],
    )


def _build_time_axis(prepared: PreparedResource) -> TimeSeriesTimeAxis:
    timestamps = prepared.frame["__time__"]
    unique = pd.Index(timestamps.unique()).sort_values()
    granularity, seconds, irregular, missing = infer_granularity(timestamps)
    keys = [*prepared.entity_fields, "__metric__", "__time__"]
    return TimeSeriesTimeAxis(
        timestamp_field=prepared.time_field,
        granularity=granularity,
        granularity_seconds=round(seconds, 6) if seconds is not None else None,
        start_time=pd.Timestamp(unique[0]).to_pydatetime() if len(unique) else None,
        end_time=pd.Timestamp(unique[-1]).to_pydatetime() if len(unique) else None,
        sample_count=len(unique),
        missing_interval_count=missing,
        duplicate_timestamp_count=max(
            len(prepared.frame) - len(prepared.frame[keys].drop_duplicates()),
            0,
        ),
        irregular_ratio=round(irregular, 6),
    )


def _build_quality(
    prepared: PreparedResource,
    time_axis: TimeSeriesTimeAxis,
    features: list[ResourceMetricFeature],
) -> ResourceQuality:
    total_valid = sum(item.statistics.valid_count for item in features)
    total_count = sum(item.statistics.count for item in features)
    completeness = total_valid / total_count if total_count else 0.0
    issues: list[str] = []
    if prepared.entity_fields == []:
        issues.append("未识别到实体字段")
    if time_axis.duplicate_timestamp_count:
        issues.append("存在重复时间戳")
    if time_axis.irregular_ratio > 0.1:
        issues.append("采样间隔不规则")
    if completeness < 0.9:
        issues.append("数据完整率低于90%")
    return ResourceQuality(
        row_count=len(prepared.frame),
        entity_count=len({_entity_key(row, prepared.entity_fields) for row in prepared.frame.to_dict("records")}),
        metric_count=prepared.metric_count,
        completeness=round(completeness, 6),
        duplicate_timestamp_count=time_axis.duplicate_timestamp_count,
        irregular_ratio=time_axis.irregular_ratio,
        issues=issues,
    )


def _entity_groups(prepared: PreparedResource) -> list[tuple[str, pd.DataFrame]]:
    if not prepared.entity_fields:
        return [("ALL", prepared.frame)]
    result = []
    for values, group in prepared.frame.groupby(prepared.entity_fields, sort=True):
        if not isinstance(values, tuple):
            values = (values,)
        key = "|".join(
            f"{field}={_json_value(value)}"
            for field, value in zip(prepared.entity_fields, values, strict=True)
        )
        result.append((key, group.copy()))
    return result


def _build_entity(
    entity_key: str,
    group: pd.DataFrame,
    entity_fields: list[str],
) -> TimeSeriesEntity:
    values = {}
    if entity_fields:
        first = group.iloc[0]
        values = {field: _json_value(first[field]) for field in entity_fields}
    return TimeSeriesEntity(
        entity_key=entity_key,
        fields=entity_fields,
        values=values,
    )


def _entity_key(row: dict, fields: list[str]) -> str:
    if not fields:
        return "ALL"
    return "|".join(f"{field}={row[field]}" for field in fields)


def _json_value(value):
    if pd.isna(value):
        return None
    return value.item() if hasattr(value, "item") else value


def _finite(value) -> float | None:
    if value is None:
        return None
    number = float(value)
    return round(number, 6) if math.isfinite(number) else None
