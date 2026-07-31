import pandas as pd
import numpy as np

def classify_compression_shape(
    upper_slope,
    lower_slope,
    flat_threshold=0.08,
    channel_threshold=0.15,
    triangle_min_opposite_slope=0.04,
    parallel_threshold=0.05,
):
    upper_flat = abs(upper_slope) <= flat_threshold
    lower_flat = abs(lower_slope) <= flat_threshold

    same_direction = (
        upper_slope > flat_threshold and lower_slope > flat_threshold
    ) or (
        upper_slope < -flat_threshold and lower_slope < -flat_threshold
    )

    opposite_converging = (
        upper_slope < -triangle_min_opposite_slope
        and lower_slope > triangle_min_opposite_slope
    )

    parallel = abs(upper_slope - lower_slope) <= parallel_threshold

    # Rango horizontal / casi horizontal
    if upper_flat and lower_flat:
        return "horizontal_range"

    # Triángulo simétrico: techo baja, piso sube
    if opposite_converging:
        return "symmetrical_triangle"

    # Triángulo ascendente: techo plano, piso sube
    if upper_flat and lower_slope > triangle_min_opposite_slope:
        return "ascending_triangle"

    # Triángulo descendente: techo baja, piso plano
    if upper_slope < -triangle_min_opposite_slope and lower_flat:
        return "descending_triangle"

    # Canal alcista: ambas pendientes suben, son paralelas y la pendiente es relevante
    if (
        upper_slope > channel_threshold
        and lower_slope > channel_threshold
        and parallel
    ):
        return "ascending_channel"

    # Canal bajista: ambas pendientes bajan, son paralelas y la pendiente es relevante
    if (
        upper_slope < -channel_threshold
        and lower_slope < -channel_threshold
        and parallel
    ):
        return "descending_channel"

    # Ambas suben o bajan pero muy débil → rango inclinado débil
    if same_direction:
        if upper_slope > 0 and lower_slope > 0:
            return "weak_ascending_range"
        if upper_slope < 0 and lower_slope < 0:
            return "weak_descending_range"

    return "irregular"

def compute_compression_features(recent, compression_high, compression_low):
    duration = len(recent)

    compression_height = compression_high - compression_low
    
    if compression_high <= compression_low:
        return {
            "compression_height_pct": 0,
            "compression_duration": int(duration),

            "upper_slope": 0,
            "lower_slope": 0,
            "slope_difference": 0,

            "touches_high": 0,
            "touches_low": 0,
            "touches_high_ratio": 0,
            "touches_low_ratio": 0,
            "touch_imbalance": 0,
            "touch_imbalance_ratio": 0,

            "inside_ratio": 0,
            "compression_shape": "invalid_range",
            "compression_quality_label": "BAD_SHAPE",
        }
        
    compression_height_pct = (
        compression_height / compression_low * 100
        if compression_low > 0
        else 999
    )

    x = np.arange(duration)

    upper_raw_slope = np.polyfit(x, recent["high"].values, 1)[0]
    lower_raw_slope = np.polyfit(x, recent["low"].values, 1)[0]

    avg_price = recent["close"].mean()

    upper_slope = upper_raw_slope / avg_price * 100
    lower_slope = lower_raw_slope / avg_price * 100

    slope_difference = abs(upper_slope - lower_slope)

    tolerance_price = compression_height * 0.15

    touches_high = (
        (recent["high"] >= compression_high - tolerance_price)
        & (recent["high"] <= compression_high + tolerance_price)
    ).sum()

    touches_low = (
        (recent["low"] <= compression_low + tolerance_price)
        & (recent["low"] >= compression_low - tolerance_price)
    ).sum()
    
    # Diferencia direccional:
    # positivo = más toques al techo
    # negativo = más toques al piso
    touch_imbalance = touches_high - touches_low

    # Ratios normalizados por duración para poder comparar
    # compresiones de 10, 15 y 20 velas.
    touches_high_ratio = (
        touches_high / duration
        if duration > 0
        else 0
    )

    touches_low_ratio = (
        touches_low / duration
        if duration > 0
        else 0
    )

    touch_imbalance_ratio = (
        touch_imbalance / duration
        if duration > 0
        else 0
    )

    inside_ratio = (
        (recent["high"] <= compression_high)
        & (recent["low"] >= compression_low)
    ).mean()

    compression_shape = classify_compression_shape(
        upper_slope,
        lower_slope,
    )

    good_shapes = {
        "horizontal_range",
        "symmetrical_triangle",
        "ascending_triangle",
    }

    ok_shapes = {
        "descending_triangle",
        "weak_ascending_range",
        "weak_descending_range",
    }

    if (
        compression_shape in good_shapes
        and touches_high >= 2
        and touches_low >= 2
    ):
        compression_quality_label = "GOOD_SHAPE"

    elif compression_shape in good_shapes.union(ok_shapes):
        compression_quality_label = "OK_SHAPE"

    else:
        compression_quality_label = "BAD_SHAPE"

    return {
        "compression_height_pct": round(float(compression_height_pct), 4),
        "compression_duration": int(duration),
        "upper_slope": round(float(upper_slope), 6),
        "lower_slope": round(float(lower_slope), 6),
        "slope_difference": round(float(slope_difference), 6),
        "touches_high": int(touches_high),
        "touches_low": int(touches_low),

        "touches_high_ratio": round(
            float(touches_high_ratio),
            4,
        ),
        "touches_low_ratio": round(
            float(touches_low_ratio),
            4,
        ),

        "touch_imbalance": int(touch_imbalance),
        "touch_imbalance_ratio": round(
            float(touch_imbalance_ratio),
            4,
        ),

        "inside_ratio": round(float(inside_ratio), 4),
        "compression_shape": compression_shape,
        "compression_quality_label": compression_quality_label,
    }

def detect_compression(
    df: pd.DataFrame,
    lookback: int = 10,
    base_lookback: int = 40,
    max_range_ratio: float = 0.75,
    max_atr_ratio: float = 0.85,
    max_volume_ratio: float = 1.10,
    max_body_pct: float = 0.55,
    min_score: int = 3,
):
    if df is None or len(df) < base_lookback + lookback:
        return {
            "is_compression": False,
            "score": 0,
            "reason": "not_enough_data",
        }

    d = df.copy()

    required_cols = ["open", "high", "low", "close", "volume"]
    missing = [c for c in required_cols if c not in d.columns]

    if missing:
        return {
            "is_compression": False,
            "score": 0,
            "reason": f"missing_cols:{missing}",
        }

    for col in required_cols:
        d[col] = pd.to_numeric(d[col], errors="coerce")

    d = d.dropna(subset=required_cols).reset_index(drop=True)

    if len(d) < base_lookback + lookback:
        return {
            "is_compression": False,
            "score": 0,
            "reason": "not_enough_clean_data",
        }

    d["range"] = d["high"] - d["low"]
    d["body"] = (d["close"] - d["open"]).abs()
    d["body_pct"] = d["body"] / d["range"].replace(0, 1e-12)

    d["prev_close"] = d["close"].shift(1)

    tr1 = d["high"] - d["low"]
    tr2 = (d["high"] - d["prev_close"]).abs()
    tr3 = (d["low"] - d["prev_close"]).abs()

    d["tr"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    d["atr"] = d["tr"].rolling(14).mean()

    # Compresión candidata
    recent = d.tail(lookback).copy()

    # Referencia anterior independiente
    base = d.iloc[
        -(base_lookback + lookback):-lookback
    ].copy()

    if len(recent) != lookback or len(base) != base_lookback:
        return {
            "is_compression": False,
            "score": 0,
            "reason": "invalid_compression_windows",
            "lookback": lookback,
            "base_lookback": base_lookback,
            "recent_candles": len(recent),
            "base_candles": len(base),
        }

    recent_range_avg = recent["range"].mean()
    base_range_avg = base["range"].mean()

    recent_atr = recent["atr"].mean()
    base_atr = base["atr"].mean()

    recent_volume_avg = recent["volume"].mean()
    base_volume_avg = base["volume"].mean()

    recent_body_pct = recent["body_pct"].mean()

    range_ratio = (
        recent_range_avg / base_range_avg
        if base_range_avg and base_range_avg > 0
        else 999
    )

    atr_ratio = (
        recent_atr / base_atr
        if base_atr and base_atr > 0
        else 999
    )

    volume_ratio = (
        recent_volume_avg / base_volume_avg
        if base_volume_avg and base_volume_avg > 0
        else 999
    )

    compression_high = recent["high"].max()
    compression_low = recent["low"].min()
    close = float(d["close"].iloc[-1])

    compression_range_pct = (
        (compression_high - compression_low) / close * 100
        if close > 0
        else 999
    )
    
    features = compute_compression_features(
        recent=recent,
        compression_high=compression_high,
        compression_low=compression_low,
    )

    score = 0
    reasons = []

    if range_ratio <= max_range_ratio:
        score += 1
        reasons.append("range_contracting")

    if atr_ratio <= max_atr_ratio:
        score += 1
        reasons.append("atr_contracting")

    if volume_ratio <= max_volume_ratio:
        score += 1
        reasons.append("volume_not_expanding")

    if recent_body_pct <= max_body_pct:
        score += 1
        reasons.append("small_bodies")

    is_compression = score >= min_score

    return {
        "is_compression": is_compression,
        "score": score,
        "reason": "compression" if is_compression else "no_compression",
        "reasons": reasons,

        "lookback": lookback,
        "base_lookback": base_lookback,

        "range_ratio": round(float(range_ratio), 4),
        "atr_ratio": round(float(atr_ratio), 4),
        "volume_ratio": round(float(volume_ratio), 4),
        "avg_body_pct": round(float(recent_body_pct), 4),

        "compression_high": float(compression_high),
        "compression_low": float(compression_low),
        "compression_range_pct": round(float(compression_range_pct), 4),
        
        **features,

        "recent_range_avg": float(recent_range_avg),
        "base_range_avg": float(base_range_avg),
        "recent_atr": float(recent_atr),
        "base_atr": float(base_atr),
        "recent_volume_avg": float(recent_volume_avg),
        "base_volume_avg": float(base_volume_avg),
    }