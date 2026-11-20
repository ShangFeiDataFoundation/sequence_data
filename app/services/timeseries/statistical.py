"""确定性时序分析算法。"""

from __future__ import annotations

import math
from datetime import datetime

import numpy as np
import pandas as pd

from ...config import Settings
from ...schemas import (
    TimeSeriesAnomaly,
    TimeSeriesChangePoint,
    TimeSeriesDecisionStatus,
    TimeSeriesEvent,
    TimeSeriesMetricProfile,
    TimeSeriesRelationship,
    TimeSeriesSeasonality,
    TimeSeriesSegment,
    TimeSeriesTrend,
)


def _finite(value: float | np.floating | None) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _timestamp(value) -> datetime:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.to_pydatetime()


def granularity_label(seconds: float | None) -> str | None:
    if seconds is None or seconds <= 0:
        return None
    units = (
        (86400 * 30, "1mo"),
        (86400 * 7, "1w"),
        (86400, "1d"),
        (3600, "1h"),
        (60, "1m"),
        (1, "1s"),
    )
    for unit_seconds, label in units:
        if seconds >= unit_seconds * 0.8:
            count = seconds / unit_seconds
            if abs(count - round(count)) < 0.15 and round(count) > 1:
                return f"{round(count)}{label[1:]}"
            return label
    return f"{seconds:.3f}s"


def infer_granularity(timestamps: pd.Series) -> tuple[str | None, float | None, float, int]:
    clean = pd.Index(pd.to_datetime(timestamps).dropna().unique()).sort_values()
    if len(clean) < 2:
        return None, None, 1.0, 0
    diffs = np.diff(clean.view("int64")) / 1_000_000_000
    positive = diffs[diffs > 0]
    if positive.size == 0:
        return None, None, 1.0, 0
    median = float(np.median(positive))
    irregular = float(np.mean(np.abs(positive - median) > max(median * 0.5, 1e-9)))
    missing = 0
    if median > 0:
        expected = round((clean[-1] - clean[0]).total_seconds() / median) + 1
        if expected <= 1_000_000:
            missing = max(expected - len(clean), 0)
    return granularity_label(median), median, irregular, missing


def profile_series(series: pd.Series) -> TimeSeriesMetricProfile:
    raw = pd.to_numeric(series, errors="coerce")
    clean = raw.dropna()
    if clean.empty:
        return TimeSeriesMetricProfile(count=0, missing_count=int(raw.isna().sum()), missing_rate=1.0)
    median = _finite(clean.median())
    first = _finite(clean.iloc[0])
    last = _finite(clean.iloc[-1])
    absolute = None if first is None or last is None else last - first
    relative = None
    if first not in (None, 0) and absolute is not None:
        relative = absolute / abs(first)
    return TimeSeriesMetricProfile(
        count=int(clean.count()),
        missing_count=int(raw.isna().sum()),
        missing_rate=round(float(raw.isna().mean()), 6),
        min=_finite(clean.min()),
        max=_finite(clean.max()),
        mean=_finite(clean.mean()),
        median=median,
        std=_finite(clean.std(ddof=0)),
        first_value=first,
        last_value=last,
        absolute_change=_finite(absolute),
        relative_change=_finite(relative),
        quantiles={
            "p25": _finite(clean.quantile(0.25)) or 0.0,
            "p50": median or 0.0,
            "p75": _finite(clean.quantile(0.75)) or 0.0,
        },
    )


def analyze_trend(series: pd.Series) -> TimeSeriesTrend | None:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if len(clean) < 3:
        return None
    x = np.linspace(0.0, 1.0, len(clean))
    y = clean.to_numpy(dtype=float)
    slope = float(np.polyfit(x, y, 1)[0])
    value_range = max(float(np.max(y) - np.min(y)), 1e-9)
    normalized = slope / value_range
    correlation = float(np.corrcoef(x, y)[0, 1]) if np.std(y) > 0 else 0.0
    if not math.isfinite(correlation):
        correlation = 0.0
    if abs(normalized) < 0.08:
        direction = "FLAT"
    elif slope > 0:
        direction = "UP"
    else:
        direction = "DOWN"
    return TimeSeriesTrend(
        direction=direction,
        slope=round(slope, 6),
        normalized_slope=round(normalized, 6),
        strength=round(min(abs(correlation), 1.0), 6),
    )


def analyze_seasonalities(
    series: pd.Series,
    *,
    granularity_seconds: float | None,
    max_results: int = 3,
) -> list[TimeSeriesSeasonality]:
    clean = pd.to_numeric(series, errors="coerce").interpolate(limit_direction="both")
    values = clean.dropna().to_numpy(dtype=float)
    if len(values) < 8 or np.std(values) < 1e-9:
        return []
    window = min(max(3, len(values) // 10), 31)
    trend = pd.Series(values).rolling(window, center=True, min_periods=1).mean().to_numpy()
    residual = values - trend
    max_lag = min(len(values) // 2, 512)
    candidates: list[tuple[float, int, float]] = []
    expected_periods: set[int] = set()
    if granularity_seconds:
        for seconds in (3600, 1800, 900, 300, 60):
            if math.isclose(granularity_seconds, seconds, rel_tol=0.05):
                expected_periods = {
                    max(2, round(86400 / seconds)),
                    max(2, round(7 * 86400 / seconds)),
                }
                break
    for lag in range(2, max_lag + 1):
        left = residual[:-lag]
        right = residual[lag:]
        if len(left) < 3 or np.std(left) < 1e-9 or np.std(right) < 1e-9:
            continue
        corr = float(np.corrcoef(left, right)[0, 1])
        if math.isfinite(corr) and corr >= 0.35:
            adjusted = (
                corr
                + (0.08 if lag in expected_periods else 0.0)
                - 0.02 * math.log2(lag)
            )
            candidates.append((adjusted, lag, corr))
    candidates.sort(reverse=True)
    selected: list[tuple[float, int]] = []
    for _, lag, corr in candidates:
        if any(abs(lag - other) <= max(1, other // 10) for _, other in selected):
            continue
        if any(lag % other == 0 for _, other in selected):
            continue
        selected.append((corr, lag))
        if len(selected) >= max_results:
            break
    result: list[TimeSeriesSeasonality] = []
    for corr, lag in selected:
        seconds = lag * granularity_seconds if granularity_seconds else None
        result.append(
            TimeSeriesSeasonality(
                period_samples=lag,
                period_seconds=round(seconds, 3) if seconds is not None else None,
                period_label=granularity_label(seconds) if seconds else f"{lag} samples",
                autocorrelation=round(corr, 6),
                strength=round(min(max(corr, 0.0), 1.0), 6),
            )
        )
    return result


def detect_change_points(series: pd.Series, max_results: int = 10) -> list[TimeSeriesChangePoint]:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if len(clean) < 8:
        return []
    window = max(2, min(30, len(clean) // 8))
    values = clean.to_numpy(dtype=float)
    scores: list[tuple[float, int, float, float]] = []
    for index in range(window, len(values) - window + 1):
        before = values[index - window:index]
        after = values[index:index + window]
        before_mean = float(np.mean(before))
        after_mean = float(np.mean(after))
        difference = abs(after_mean - before_mean)
        scores.append((difference, index, before_mean, after_mean))
    if not scores:
        return []
    score_values = np.asarray([item[0] for item in scores], dtype=float)
    threshold = float(np.mean(score_values) + 2.5 * np.std(score_values))
    threshold = max(threshold, float(np.percentile(score_values, 80)))
    selected: list[tuple[float, int, float, float]] = []
    for item in sorted(scores, reverse=True):
        if item[0] < threshold or item[0] <= 0:
            continue
        if any(abs(item[1] - other[1]) < window for other in selected):
            continue
        selected.append(item)
        if len(selected) >= max_results:
            break
    selected.sort(key=lambda item: item[1])
    result: list[TimeSeriesChangePoint] = []
    for score, index, before, after in selected:
        result.append(
            TimeSeriesChangePoint(
                timestamp=_timestamp(clean.index[index]),
                score=round(float(score), 6),
                before_mean=round(before, 6),
                after_mean=round(after, 6),
                direction="UP" if after >= before else "DOWN",
            )
        )
    return result


def build_segments(series: pd.Series, max_results: int = 200) -> list[TimeSeriesSegment]:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if len(clean) < 3:
        return []
    p33, p66 = clean.quantile([0.33, 0.66]).tolist()
    if math.isclose(float(p33), float(p66), rel_tol=1e-9, abs_tol=1e-9):
        return []
    states = [
        "LOW" if value <= p33 else "HIGH" if value >= p66 else "NORMAL"
        for value in clean.to_numpy(dtype=float)
    ]
    segments: list[TimeSeriesSegment] = []
    start = 0
    for index in range(1, len(states) + 1):
        if index < len(states) and states[index] == states[start]:
            continue
        block = clean.iloc[start:index]
        if len(block) >= 2:
            segments.append(
                TimeSeriesSegment(
                    state=states[start],
                    start_time=_timestamp(block.index[0]),
                    end_time=_timestamp(block.index[-1]),
                    duration_seconds=max(
                        (pd.Timestamp(block.index[-1]) - pd.Timestamp(block.index[0])).total_seconds(),
                        0.0,
                    ),
                    mean=round(float(block.mean()), 6),
                    max=round(float(block.max()), 6),
                    min=round(float(block.min()), 6),
                )
            )
        start = index
        if len(segments) >= max_results:
            break
    return segments


def detect_anomalies(
    series: pd.Series,
    *,
    settings: Settings,
    max_results: int = 50,
) -> list[TimeSeriesAnomaly]:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if len(clean) < 8:
        return []
    window = max(3, min(31, len(clean) // 5))
    if isinstance(clean.index, pd.DatetimeIndex) and len(clean) >= 24:
        buckets = clean.index.hour
        expected = clean.groupby(buckets).transform("median")
    else:
        expected = clean.rolling(window, center=True, min_periods=1).median()
    residual = clean - expected
    median = float(residual.median())
    mad = float((residual - median).abs().median())
    scale = 1.4826 * mad
    if scale < 1e-9:
        scale = float(residual.std(ddof=0))
    if scale < 1e-9:
        return []
    zscores = (residual - median).abs() / scale
    flagged = zscores[zscores >= settings.timeseries_anomaly_zscore]
    if flagged.empty:
        return []

    groups: list[list[int]] = []
    current: list[int] = []
    positions = [clean.index.get_loc(index) for index in flagged.index]
    for position in positions:
        if current and position > current[-1] + 1:
            groups.append(current)
            current = []
        current.append(position)
    if current:
        groups.append(current)

    anomalies: list[TimeSeriesAnomaly] = []
    for group in groups[:max_results]:
        block = clean.iloc[group]
        index = zscores.iloc[group].idxmax()
        anomalies.append(
            TimeSeriesAnomaly(
                start_time=_timestamp(block.index[0]),
                end_time=_timestamp(block.index[-1]),
                score=round(float(zscores.loc[index]), 6),
                observed_value=round(float(clean.loc[index]), 6),
                expected_value=round(float(expected.loc[index]), 6),
                evidence=["鲁棒 Z 分数超过阈值", f"窗口={window}"],
            )
        )
    return anomalies


def build_events(
    *,
    change_points: list[TimeSeriesChangePoint],
    segments: list[TimeSeriesSegment],
    anomalies: list[TimeSeriesAnomaly],
    max_results: int = 100,
) -> list[TimeSeriesEvent]:
    events: list[TimeSeriesEvent] = []
    for point in change_points:
        events.append(
            TimeSeriesEvent(
                event_type="CHANGE_UP" if point.direction == "UP" else "CHANGE_DOWN",
                start_time=point.timestamp,
                end_time=point.timestamp,
                magnitude=point.score,
                direction=point.direction,
                confidence=min(0.95, 0.55 + point.score / max(point.score + 1.0, 1e-9)),
                evidence=["检测到前后窗口均值发生显著变化"],
            )
        )
    for segment in segments:
        if segment.state == "HIGH":
            events.append(
                TimeSeriesEvent(
                    event_type="HIGH_LOAD",
                    start_time=segment.start_time,
                    end_time=segment.end_time,
                    magnitude=segment.mean,
                    direction="UP",
                    confidence=0.78,
                    evidence=["连续高值区间"],
                )
            )
        elif segment.state == "LOW":
            events.append(
                TimeSeriesEvent(
                    event_type="LOW_LOAD",
                    start_time=segment.start_time,
                    end_time=segment.end_time,
                    magnitude=segment.mean,
                    direction="DOWN",
                    confidence=0.72,
                    evidence=["连续低值区间"],
                )
            )
    for anomaly in anomalies:
        direction = "UP"
        if anomaly.expected_value is not None and anomaly.observed_value < anomaly.expected_value:
            direction = "DOWN"
        events.append(
            TimeSeriesEvent(
                event_type="SPIKE" if direction == "UP" else "DROP",
                start_time=anomaly.start_time,
                end_time=anomaly.end_time,
                magnitude=anomaly.score,
                direction=direction,
                confidence=min(0.98, 0.55 + anomaly.score / (anomaly.score + 3.0)),
                evidence=anomaly.evidence,
            )
        )
    return events[:max_results]


def metric_relationships(
    frame: pd.DataFrame,
    *,
    entity_key: str,
    threshold: float,
    max_lag: int = 3,
    max_results: int = 50,
) -> list[TimeSeriesRelationship]:
    if frame.shape[1] < 2 or len(frame) < 5:
        return []
    numeric = frame.apply(pd.to_numeric, errors="coerce")
    relationships: list[TimeSeriesRelationship] = []
    columns = list(numeric.columns)
    for left_index, left in enumerate(columns):
        for right in columns[left_index + 1:]:
            best: tuple[float, int] | None = None
            pair = numeric[[left, right]].dropna()
            if len(pair) < 5:
                continue
            for lag in range(-max_lag, max_lag + 1):
                aligned = pd.DataFrame(
                    {
                        "left": pair[left],
                        "right": pair[right].shift(lag),
                    }
                ).dropna()
                if len(aligned) < 4:
                    continue
                corr = float(aligned["left"].corr(aligned["right"]))
                if not math.isfinite(corr):
                    continue
                candidate = (corr, lag)
                if best is None or abs(candidate[0]) > abs(best[0]):
                    best = candidate
            if best is None or abs(best[0]) < threshold:
                continue
            corr, lag = best
            if lag == 0:
                relation_type = "CO_MOVING"
            elif lag > 0:
                relation_type = "LEADS"
            else:
                relation_type = "LAGS"
            relationships.append(
                TimeSeriesRelationship(
                    entity_key=entity_key,
                    from_metric=left,
                    to_metric=right,
                    relation_type=relation_type,
                    lag_samples=lag,
                    correlation=round(corr, 6),
                    confidence=round(min(abs(corr), 1.0), 6),
                    decision=(
                        TimeSeriesDecisionStatus.AUTO_ACCEPTED
                        if abs(corr) >= 0.75
                        else TimeSeriesDecisionStatus.AUTO_ACCEPTED_WITH_ALTERNATIVES
                    ),
                )
            )
            if len(relationships) >= max_results:
                return relationships
    return relationships
