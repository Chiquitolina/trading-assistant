# tools/analyze_btc_context_days.py

import os
import argparse
from pathlib import Path

import ccxt
import pandas as pd
import numpy as np

from signals.indicators.trend_detector import detect_trend_up
from signals.indicators.compression_detector import detect_compression

# ==========================================================
# CONFIG
# ==========================================================

SYMBOL = "BTC/USDT:USDT"
OUT_SYMBOL = "BTCUSDT"

TIMEFRAMES = ["15m", "30m", "1h", "4h"]

REPORTS_DIR = Path("reports")
REPORTS_DIR.mkdir(exist_ok=True)

OUTPUT_PATH = REPORTS_DIR / "btc_context_days.csv"


# ==========================================================
# EXCHANGE
# ==========================================================

exchange = ccxt.binanceusdm({
    "enableRateLimit": True,
})


# ==========================================================
# DATA
# ==========================================================

def fetch_ohlcv(symbol: str, timeframe: str, days: int) -> pd.DataFrame:
    limit = 1500

    now = exchange.milliseconds()
    since = now - days * 24 * 60 * 60 * 1000

    all_rows = []

    while True:
        rows = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=limit)

        if not rows:
            break

        all_rows.extend(rows)

        last_ts = rows[-1][0]
        since = last_ts + 1

        if last_ts >= now:
            break

        if len(rows) < limit:
            break

    df = pd.DataFrame(
        all_rows,
        columns=["timestamp", "open", "high", "low", "close", "volume"]
    )

    df = df.drop_duplicates("timestamp")
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df["date"] = df["datetime"].dt.date.astype(str)

    # Binance OHLCV volume es base volume.
    # Aproximamos quote volume.
    df["quote_volume"] = df["volume"] * df["close"]

    return df.reset_index(drop=True)


# ==========================================================
# INDICATORS
# ==========================================================

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["ema99"] = df["close"].ewm(span=99, adjust=False).mean()

    prev_close = df["close"].shift(1)

    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"] - prev_close).abs()

    df["tr"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr14"] = df["tr"].rolling(14).mean()
    df["atr_pct"] = df["atr14"] / df["close"] * 100

    df["volume_ma20"] = df["quote_volume"].rolling(20).mean()
    df["volume_ratio"] = df["quote_volume"] / df["volume_ma20"]

    df["range_pct"] = (df["high"] - df["low"]) / df["close"] * 100
    df["body_pct"] = (df["close"] - df["open"]).abs() / df["close"] * 100
    df["candle_return_pct"] = df["close"].pct_change() * 100

    df["green"] = df["close"] > df["open"]
    df["red"] = df["close"] < df["open"]

    df["close_above_ema20"] = df["close"] > df["ema20"]
    df["close_above_ema50"] = df["close"] > df["ema50"]
    df["close_above_ema99"] = df["close"] > df["ema99"]

    df["ema20_above_ema50"] = df["ema20"] > df["ema50"]
    df["ema50_above_ema99"] = df["ema50"] > df["ema99"]

    df["ema20_slope"] = df["ema20"].pct_change(3) * 100
    df["ema50_slope"] = df["ema50"].pct_change(3) * 100
    df["ema99_slope"] = df["ema99"].pct_change(3) * 100

    df["dist_ema20_pct"] = (df["close"] - df["ema20"]) / df["close"] * 100
    df["dist_ema50_pct"] = (df["close"] - df["ema50"]) / df["close"] * 100
    df["dist_ema99_pct"] = (df["close"] - df["ema99"]) / df["close"] * 100

    df["trend_score"] = 0
    df["trend_score"] += df["close_above_ema20"].astype(int)
    df["trend_score"] += df["close_above_ema50"].astype(int)
    df["trend_score"] += df["close_above_ema99"].astype(int)
    df["trend_score"] += df["ema20_above_ema50"].astype(int)
    df["trend_score"] += df["ema50_above_ema99"].astype(int)
    df["trend_score"] += (df["ema20_slope"] > 0).astype(int)
    df["trend_score"] += (df["ema50_slope"] > 0).astype(int)

    return df

def add_real_trend_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["real_trend_up"] = False
    df["real_trend_score"] = 0
    df["real_higher_high"] = False
    df["real_higher_low"] = False
    df["real_trend_signature"] = ""

    reason_cols = [
        "close_above_ema_fast",
        "ema_fast_above_slow",
        "ema_fast_slope_up",
        "ema_slow_slope_up",
        "higher_high",
        "higher_low",
    ]

    for reason in reason_cols:
        df[f"real_reason_{reason}"] = False

    for i in range(len(df)):
        if i < 80:
            continue

        window = df.iloc[:i + 1].copy()

        try:
            result = detect_trend_up(
                window,
                lookback=20,
                ema_fast=20,
                ema_slow=50,
                min_score=4,
            )

            reasons = result.get("reasons", [])
            
            df.loc[df.index[i], "real_trend_signature"] = build_trend_signature(reasons)

            df.loc[df.index[i], "real_trend_up"] = bool(result.get("trend_up", False))
            df.loc[df.index[i], "real_trend_score"] = int(result.get("score", 0))
            df.loc[df.index[i], "real_higher_high"] = bool(result.get("higher_high", False))
            df.loc[df.index[i], "real_higher_low"] = bool(result.get("higher_low", False))

            for reason in reason_cols:
                df.loc[df.index[i], f"real_reason_{reason}"] = reason in reasons

        except Exception:
            df.loc[df.index[i], "real_trend_up"] = False
            df.loc[df.index[i], "real_trend_score"] = 0
            df.loc[df.index[i], "real_higher_high"] = False
            df.loc[df.index[i], "real_higher_low"] = False

            for reason in reason_cols:
                df.loc[df.index[i], f"real_reason_{reason}"] = False

    return df

def build_compression_signature(reasons):
    order = [
        ("range_contracting", "R"),
        ("atr_contracting", "A"),
        ("volume_not_expanding", "V"),
        ("small_bodies", "B"),
    ]

    return "+".join(
        short
        for full, short in order
        if full in reasons
    )
    
def build_trend_signature(reasons):
    order = [
        ("close_above_ema_fast", "E"),
        ("ema_fast_above_slow", "F"),
        ("ema_fast_slope_up", "S"),
        ("ema_slow_slope_up", "L"),
        ("higher_high", "H"),
        ("higher_low", "HL"),
    ]

    return "+".join(
        short
        for full, short in order
        if full in reasons
    )

def add_real_compression_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["real_is_compression"] = False
    df["real_compression_score"] = 0
    df["real_compression_range_ratio"] = np.nan
    df["real_compression_atr_ratio"] = np.nan
    df["real_compression_volume_ratio"] = np.nan
    df["real_compression_avg_body_pct"] = np.nan
    df["real_compression_range_pct"] = np.nan

    df["real_compression_signature"] = ""

    reason_cols = [
        "range_contracting",
        "atr_contracting",
        "volume_not_expanding",
        "small_bodies",
    ]

    for reason in reason_cols:
        df[f"real_compression_reason_{reason}"] = False

    for i in range(len(df)):
        if i < 80:
            continue

        window = df.iloc[:i + 1].copy()

        try:
            result = detect_compression(
                window,
                lookback=10,
                base_lookback=40,
                max_range_ratio=0.75,
                max_atr_ratio=0.85,
                max_volume_ratio=1.10,
                max_body_pct=0.55,
                min_score=3,
            )

            reasons = result.get("reasons", [])
            
            signature = []

            if "range_contracting" in reasons:
                signature.append("R")

            if "atr_contracting" in reasons:
                signature.append("A")

            if "volume_not_expanding" in reasons:
                signature.append("V")

            if "small_bodies" in reasons:
                signature.append("B")

            df.loc[df.index[i], "real_compression_signature"] = build_compression_signature(reasons)

            df.loc[df.index[i], "real_is_compression"] = bool(result.get("is_compression", False))
            df.loc[df.index[i], "real_compression_score"] = int(result.get("score", 0))
            df.loc[df.index[i], "real_compression_range_ratio"] = result.get("range_ratio", np.nan)
            df.loc[df.index[i], "real_compression_atr_ratio"] = result.get("atr_ratio", np.nan)
            df.loc[df.index[i], "real_compression_volume_ratio"] = result.get("volume_ratio", np.nan)
            df.loc[df.index[i], "real_compression_avg_body_pct"] = result.get("avg_body_pct", np.nan)
            df.loc[df.index[i], "real_compression_range_pct"] = result.get("compression_range_pct", np.nan)

            for reason in reason_cols:
                df.loc[df.index[i], f"real_compression_reason_{reason}"] = reason in reasons

        except Exception:
            df.loc[df.index[i], "real_is_compression"] = False
            df.loc[df.index[i], "real_compression_score"] = 0

            for reason in reason_cols:
                df.loc[df.index[i], f"real_compression_reason_{reason}"] = False

    return df

# ==========================================================
# COMPRESSION FEATURES
# ==========================================================

def add_compression_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["rolling_high_20"] = df["high"].rolling(20).max()
    df["rolling_low_20"] = df["low"].rolling(20).min()

    df["compression_range_pct"] = (
        (df["rolling_high_20"] - df["rolling_low_20"]) / df["close"] * 100
    )

    df["range_ratio"] = df["range_pct"] / df["range_pct"].rolling(50).mean()
    df["atr_ratio"] = df["atr_pct"] / df["atr_pct"].rolling(50).mean()

    df["compression_score"] = 0
    df["compression_score"] += (df["range_ratio"] < 0.75).astype(int)
    df["compression_score"] += (df["atr_ratio"] < 0.85).astype(int)
    df["compression_score"] += (df["volume_ratio"] < 1.10).astype(int)
    df["compression_score"] += (df["compression_range_pct"] < df["compression_range_pct"].rolling(50).mean()).astype(int)

    df["is_compressed"] = df["compression_score"] >= 3

    df["breakout_up"] = (
        (df["close"] > df["rolling_high_20"].shift(1)) &
        (df["volume_ratio"] > 1.10)
    )

    df["breakout_down"] = (
        (df["close"] < df["rolling_low_20"].shift(1)) &
        (df["volume_ratio"] > 1.10)
    )

    df["breakout_any"] = df["breakout_up"] | df["breakout_down"]

    return df


# ==========================================================
# MARKET STRUCTURE
# ==========================================================

def add_structure_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["swing_high"] = (
        (df["high"] > df["high"].shift(1)) &
        (df["high"] > df["high"].shift(2)) &
        (df["high"] > df["high"].shift(-1)) &
        (df["high"] > df["high"].shift(-2))
    )

    df["swing_low"] = (
        (df["low"] < df["low"].shift(1)) &
        (df["low"] < df["low"].shift(2)) &
        (df["low"] < df["low"].shift(-1)) &
        (df["low"] < df["low"].shift(-2))
    )

    df["last_swing_high"] = df["high"].where(df["swing_high"]).ffill()
    df["last_swing_low"] = df["low"].where(df["swing_low"]).ffill()

    df["dist_swing_high_pct"] = (df["last_swing_high"] - df["close"]) / df["close"] * 100
    df["dist_swing_low_pct"] = (df["close"] - df["last_swing_low"]) / df["close"] * 100

    df["higher_high"] = df["high"] > df["high"].shift(1)
    df["higher_low"] = df["low"] > df["low"].shift(1)
    df["lower_high"] = df["high"] < df["high"].shift(1)
    df["lower_low"] = df["low"] < df["low"].shift(1)

    df["structure_score"] = 0
    df["structure_score"] += df["higher_high"].astype(int)
    df["structure_score"] += df["higher_low"].astype(int)
    df["structure_score"] -= df["lower_high"].astype(int)
    df["structure_score"] -= df["lower_low"].astype(int)

    return df


# ==========================================================
# DAILY AGGREGATION
# ==========================================================

def max_streak(series: pd.Series) -> int:
    best = 0
    current = 0

    for value in series.fillna(False):
        if value:
            current += 1
            best = max(best, current)
        else:
            current = 0

    return best


def efficiency_ratio(group: pd.DataFrame) -> float:
    if len(group) < 2:
        return np.nan

    net_move = abs(group["close"].iloc[-1] - group["open"].iloc[0])
    total_move = group["close"].diff().abs().sum()

    if total_move == 0:
        return 0

    return net_move / total_move

def last_valid(series: pd.Series, default=np.nan):
    s = series.dropna()
    if s.empty:
        return default
    return s.iloc[-1]


def safe_mean(series: pd.Series, default=np.nan):
    value = series.mean()
    return default if pd.isna(value) else value


def safe_max(series: pd.Series, default=np.nan):
    value = series.max()
    return default if pd.isna(value) else value


def safe_min(series: pd.Series, default=np.nan):
    value = series.min()
    return default if pd.isna(value) else value

def aggregate_daily(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    rows = []

    for date, g in df.groupby("date"):
        g = g.copy()

        if len(g) < 3:
            continue

        prefix = f"btc_{tf}_"

        row = {"date": date}

        row[prefix + "open"] = g["open"].iloc[0]
        row[prefix + "close"] = g["close"].iloc[-1]
        row[prefix + "high"] = g["high"].max()
        row[prefix + "low"] = g["low"].min()

        row[prefix + "return_pct"] = (g["close"].iloc[-1] - g["open"].iloc[0]) / g["open"].iloc[0] * 100
        row[prefix + "range_pct"] = (g["high"].max() - g["low"].min()) / g["close"].iloc[-1] * 100

        row[prefix + "trend_score_avg"] = g["trend_score"].mean()
        row[prefix + "trend_score_max"] = g["trend_score"].max()
        row[prefix + "trend_score_last"] = g["trend_score"].iloc[-1]
        row[prefix + "trend_score_min"] = g["trend_score"].min()
        row[prefix + "trend_score_std"] = g["trend_score"].std()
        row[prefix + "trend_score_median"] = g["trend_score"].median()
        row[prefix + "real_trend_up_pct"] = g["real_trend_up"].mean() * 100
        row[prefix + "real_trend_up_count"] = int(g["real_trend_up"].sum())
        row[prefix + "real_trend_up_last"] = bool(g["real_trend_up"].iloc[-1])
        row[prefix + "real_trend_up_max_streak"] = max_streak(g["real_trend_up"])
        row[prefix + "real_trend_score_avg"] = g["real_trend_score"].mean()
        row[prefix + "real_trend_score_max"] = g["real_trend_score"].max()
        row[prefix + "real_trend_score_last"] = int(g["real_trend_score"].iloc[-1])
        row[prefix + "real_trend_score_min"] = g["real_trend_score"].min()
        
        row[prefix + "real_higher_high_pct"] = g["real_higher_high"].mean() * 100
        row[prefix + "real_higher_low_pct"] = g["real_higher_low"].mean() * 100

        reason_cols = [
            "close_above_ema_fast",
            "ema_fast_above_slow",
            "ema_fast_slope_up",
            "ema_slow_slope_up",
            "higher_high",
            "higher_low",
        ]

        for reason in reason_cols:
            col = f"real_reason_{reason}"
            if col in g.columns:
                row[prefix + f"{reason}_pct"] = g[col].mean() * 100
                
        if "real_trend_signature" in g.columns:
            trend_signatures = (
                g["real_trend_signature"]
                .astype(str)
                .replace("", np.nan)
                .dropna()
            )

            if not trend_signatures.empty:
                counts = trend_signatures.value_counts()

                row[prefix + "dominant_trend_signature"] = counts.index[0]
                row[prefix + "dominant_trend_signature_pct"] = (
                    counts.iloc[0] / len(trend_signatures) * 100
                )
                row[prefix + "trend_signature_count"] = len(counts)
            else:
                row[prefix + "dominant_trend_signature"] = ""
                row[prefix + "dominant_trend_signature_pct"] = 0
                row[prefix + "trend_signature_count"] = 0

        row[prefix + "ema20_above_50_pct"] = g["ema20_above_ema50"].mean() * 100
        row[prefix + "ema50_above_99_pct"] = g["ema50_above_ema99"].mean() * 100

        row[prefix + "close_above_ema20_pct"] = g["close_above_ema20"].mean() * 100
        row[prefix + "close_above_ema50_pct"] = g["close_above_ema50"].mean() * 100
        row[prefix + "close_above_ema99_pct"] = g["close_above_ema99"].mean() * 100

        row[prefix + "max_streak_above_ema20"] = max_streak(g["close_above_ema20"])
        row[prefix + "max_streak_above_ema50"] = max_streak(g["close_above_ema50"])

        row[prefix + "ema20_slope_avg"] = g["ema20_slope"].mean()
        row[prefix + "ema50_slope_avg"] = g["ema50_slope"].mean()

        row[prefix + "dist_ema20_avg"] = g["dist_ema20_pct"].mean()
        row[prefix + "dist_ema50_avg"] = g["dist_ema50_pct"].mean()
        row[prefix + "dist_ema99_avg"] = g["dist_ema99_pct"].mean()

        row[prefix + "atr_pct_avg"] = g["atr_pct"].mean()
        row[prefix + "atr_pct_max"] = g["atr_pct"].max()
        row[prefix + "atr_pct_last"] = last_valid(g["atr_pct"])
        row[prefix + "atr_pct_min"] = g["atr_pct"].min()
        row[prefix + "atr_pct_std"] = g["atr_pct"].std()
        row[prefix + "atr_pct_median"] = g["atr_pct"].median()
        row[prefix + "atr_vs_mean_last"] = last_valid(g["atr_ratio"])

        row[prefix + "volume_ratio_avg"] = g["volume_ratio"].mean()
        row[prefix + "volume_ratio_max"] = g["volume_ratio"].max()
        row[prefix + "quote_volume_sum"] = g["quote_volume"].sum()
        row[prefix + "volume_ratio_min"] = g["volume_ratio"].min()
        row[prefix + "volume_ratio_std"] = g["volume_ratio"].std()
        row[prefix + "volume_ratio_median"] = g["volume_ratio"].median()

        row[prefix + "compression_count"] = int(g["is_compressed"].sum())
        row[prefix + "time_in_compression_pct"] = g["is_compressed"].mean() * 100
        row[prefix + "compression_score_avg"] = g["compression_score"].mean()
        row[prefix + "compression_score_max"] = g["compression_score"].max()
        row[prefix + "compression_score_min"] = g["compression_score"].min()
        row[prefix + "compression_score_std"] = g["compression_score"].std()
        row[prefix + "compression_score_median"] = g["compression_score"].median()
        row[prefix + "range_ratio_avg"] = g["range_ratio"].mean()
        row[prefix + "atr_ratio_avg"] = g["atr_ratio"].mean()
        
        row[prefix + "real_compression_count"] = int(g["real_is_compression"].sum())
        row[prefix + "real_time_in_compression_pct"] = g["real_is_compression"].mean() * 100
        row[prefix + "real_compression_score_avg"] = g["real_compression_score"].mean()
        row[prefix + "real_compression_score_max"] = g["real_compression_score"].max()
        row[prefix + "real_compression_score_last"] = int(g["real_compression_score"].iloc[-1])
        row[prefix + "real_compression_score_min"] = g["real_compression_score"].min()
        row[prefix + "real_compression_score_std"] = g["real_compression_score"].std()

        row[prefix + "real_compression_range_ratio_avg"] = g["real_compression_range_ratio"].mean()
        row[prefix + "real_compression_atr_ratio_avg"] = g["real_compression_atr_ratio"].mean()
        row[prefix + "real_compression_volume_ratio_avg"] = g["real_compression_volume_ratio"].mean()
        row[prefix + "real_compression_avg_body_pct_avg"] = g["real_compression_avg_body_pct"].mean()
        row[prefix + "real_compression_range_pct_avg"] = g["real_compression_range_pct"].mean()
        row[prefix + "real_compression_range_pct_min"] = safe_min(
            g["real_compression_range_pct"]
        )

        row[prefix + "real_compression_range_pct_max"] = safe_max(
            g["real_compression_range_pct"]
        )

        row[prefix + "real_compression_range_ratio_min"] = safe_min(
            g["real_compression_range_ratio"]
        )

        row[prefix + "real_compression_range_ratio_max"] = safe_max(
            g["real_compression_range_ratio"]
        )

        row[prefix + "real_compression_atr_ratio_min"] = safe_min(
            g["real_compression_atr_ratio"]
        )

        row[prefix + "real_compression_atr_ratio_max"] = safe_max(
            g["real_compression_atr_ratio"]
        )

        row[prefix + "real_compression_volume_ratio_min"] = safe_min(
            g["real_compression_volume_ratio"]
        )

        row[prefix + "real_compression_volume_ratio_max"] = safe_max(
            g["real_compression_volume_ratio"]
        )

        row[prefix + "real_compression_range_ratio_last"] = last_valid(g["real_compression_range_ratio"])
        row[prefix + "real_compression_atr_ratio_last"] = last_valid(g["real_compression_atr_ratio"])
        row[prefix + "real_compression_volume_ratio_last"] = last_valid(g["real_compression_volume_ratio"])
        row[prefix + "real_compression_avg_body_pct_last"] = last_valid(g["real_compression_avg_body_pct"])
        row[prefix + "real_compression_range_pct_last"] = last_valid(g["real_compression_range_pct"])

        compression_reason_cols = [
            "range_contracting",
            "atr_contracting",
            "volume_not_expanding",
            "small_bodies",
        ]

        for reason in compression_reason_cols:
            col = f"real_compression_reason_{reason}"
            if col in g.columns:
                row[prefix + f"real_compression_{reason}_pct"] = g[col].mean() * 100
                
        if "real_compression_signature" in g.columns:
            signatures = (
                g["real_compression_signature"]
                .astype(str)
                .replace("", np.nan)
                .dropna()
            )

            if not signatures.empty:
                counts = signatures.value_counts()

                row[prefix + "dominant_compression_signature"] = counts.index[0]
                row[prefix + "dominant_compression_signature_pct"] = (
                    counts.iloc[0] / len(signatures) * 100
                )
                row[prefix + "compression_signature_count"] = len(counts)
            else:
                row[prefix + "dominant_compression_signature"] = ""
                row[prefix + "dominant_compression_signature_pct"] = 0
                row[prefix + "compression_signature_count"] = 0

        row[prefix + "breakout_count"] = int(g["breakout_any"].sum())
        row[prefix + "breakout_up_count"] = int(g["breakout_up"].sum())
        row[prefix + "breakout_down_count"] = int(g["breakout_down"].sum())

        breakout_g = g[g["breakout_any"]]
        row[prefix + "breakout_volume_ratio_avg"] = breakout_g["volume_ratio"].mean() if not breakout_g.empty else 0

        row[prefix + "green_pct"] = g["green"].mean() * 100
        row[prefix + "red_pct"] = g["red"].mean() * 100
        row[prefix + "largest_green_pct"] = g.loc[g["green"], "candle_return_pct"].max() if g["green"].any() else 0
        row[prefix + "largest_red_pct"] = g.loc[g["red"], "candle_return_pct"].min() if g["red"].any() else 0

        row[prefix + "higher_high_count"] = int(g["higher_high"].sum())
        row[prefix + "higher_low_count"] = int(g["higher_low"].sum())
        row[prefix + "lower_high_count"] = int(g["lower_high"].sum())
        row[prefix + "lower_low_count"] = int(g["lower_low"].sum())
        row[prefix + "structure_score_avg"] = g["structure_score"].mean()

        row[prefix + "dist_swing_high_avg"] = g["dist_swing_high_pct"].mean()
        row[prefix + "dist_swing_low_avg"] = g["dist_swing_low_pct"].mean()
        row[prefix + "dist_swing_high_last"] = last_valid(g["dist_swing_high_pct"])
        row[prefix + "dist_swing_low_last"] = last_valid(g["dist_swing_low_pct"])

        row[prefix + "efficiency_ratio"] = efficiency_ratio(g)

        rows.append(row)

    return pd.DataFrame(rows)


# ==========================================================
# CROSS TIMEFRAME FEATURES
# ==========================================================

def add_cross_tf_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    if "btc_30m_atr_pct_avg" in df.columns and "btc_1h_atr_pct_avg" in df.columns:
        df["btc_atr_30m_vs_1h"] = df["btc_30m_atr_pct_avg"] / df["btc_1h_atr_pct_avg"]

    if "btc_15m_atr_pct_avg" in df.columns and "btc_30m_atr_pct_avg" in df.columns:
        df["btc_atr_15m_vs_30m"] = df["btc_15m_atr_pct_avg"] / df["btc_30m_atr_pct_avg"]

    if "btc_30m_trend_score_avg" in df.columns and "btc_1h_trend_score_avg" in df.columns:
        df["btc_trend_30m_1h_alignment"] = (
            df["btc_30m_trend_score_avg"] + df["btc_1h_trend_score_avg"]
        ) / 2

    if "btc_1h_trend_score_avg" in df.columns and "btc_4h_trend_score_avg" in df.columns:
        df["btc_trend_1h_4h_alignment"] = (
            df["btc_1h_trend_score_avg"] + df["btc_4h_trend_score_avg"]
        ) / 2

    return df


# ==========================================================
# REGIME LABELS
# ==========================================================

def add_simple_regime_labels(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    conditions = []

    for _, row in df.iterrows():
        trend_30m = row.get("btc_30m_trend_score_avg", 0)
        trend_1h = row.get("btc_1h_trend_score_avg", 0)
        ret_30m = row.get("btc_30m_return_pct", 0)
        er_30m = row.get("btc_30m_efficiency_ratio", 0)
        atr_30m = row.get("btc_30m_atr_pct_avg", 0)

        if trend_30m >= 5 and trend_1h >= 5 and ret_30m > 0:
            regime = "bullish"
        elif trend_30m <= 2.5 and trend_1h <= 2.5 and ret_30m < 0:
            regime = "bearish"
        elif er_30m < 0.25:
            regime = "choppy"
        elif atr_30m > 1.0:
            regime = "volatile"
        else:
            regime = "range"

        conditions.append(regime)

    df["btc_context_regime_auto"] = conditions

    return df


# ==========================================================
# BOT TRADES OPTIONAL
# ==========================================================

def load_bot_trades(path: str | None) -> pd.DataFrame | None:
    if not path:
        return None

    if not os.path.exists(path):
        print(f"[WARN] trades file not found: {path}")
        return None

    df = pd.read_csv(path)

    # Intentamos detectar columna de fecha.
    possible_ts_cols = [
        "entry_ts",
        "entry_time",
        "timestamp",
        "opened_at",
        "created_at",
    ]

    ts_col = None
    for c in possible_ts_cols:
        if c in df.columns:
            ts_col = c
            break

    if ts_col is None:
        print("[WARN] no timestamp column found in trades file")
        return None

    if np.issubdtype(df[ts_col].dtype, np.number):
        df["entry_dt"] = pd.to_datetime(df[ts_col], unit="ms", utc=True, errors="coerce")
    else:
        df["entry_dt"] = pd.to_datetime(df[ts_col], utc=True, errors="coerce")

    df["date"] = df["entry_dt"].dt.date.astype(str)

    return df


def add_bot_metrics(context_df: pd.DataFrame, trades_df: pd.DataFrame | None) -> pd.DataFrame:
    if trades_df is None or trades_df.empty:
        return context_df

    df = context_df.copy()

    pnl_col = None
    for c in ["net_pnl", "pnl", "net_pnl_pct", "return_pct", "pnl_pct"]:
        if c in trades_df.columns:
            pnl_col = c
            break

    side_col = "side" if "side" in trades_df.columns else None

    rows = []

    for date, g in trades_df.groupby("date"):
        row = {"date": date}

        row["bot_trades"] = len(g)

        if side_col:
            row["bot_longs"] = int((g[side_col].astype(str).str.upper() == "LONG").sum())
            row["bot_shorts"] = int((g[side_col].astype(str).str.upper() == "SHORT").sum())

        if pnl_col:
            pnl = pd.to_numeric(g[pnl_col], errors="coerce").dropna()

            wins = pnl[pnl > 0]
            losses = pnl[pnl < 0]

            row["bot_net"] = pnl.sum()
            row["bot_winrate"] = len(wins) / len(pnl) * 100 if len(pnl) else np.nan
            row["bot_avg_win"] = wins.mean() if len(wins) else 0
            row["bot_avg_loss"] = losses.mean() if len(losses) else 0
            row["bot_expectancy"] = pnl.mean() if len(pnl) else np.nan

            gross_win = wins.sum()
            gross_loss = abs(losses.sum())

            row["bot_profit_factor"] = gross_win / gross_loss if gross_loss > 0 else np.nan

        rows.append(row)

    bot_df = pd.DataFrame(rows)

    df = df.merge(bot_df, on="date", how="left")

    bot_cols = [c for c in df.columns if c.startswith("bot_")]
    df[bot_cols] = df[bot_cols].fillna(0)

    return df


# ==========================================================
# MAIN
# ==========================================================

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--trades", type=str, default=None)
    parser.add_argument("--output", type=str, default=str(OUTPUT_PATH))
    parser.add_argument("--warmup-days", type=int, default=10)

    args = parser.parse_args()

    daily_dfs = []

    for tf in TIMEFRAMES:
        print(f"[BTC CONTEXT] Fetching {OUT_SYMBOL} {tf}...")

        df = fetch_ohlcv(SYMBOL, tf, args.days + args.warmup_days)

        print(f"[BTC CONTEXT] {tf} candles: {len(df)}")

        df = add_indicators(df)
        df = add_real_trend_features(df)
        df = add_real_compression_features(df)
        df = add_compression_features(df)
        df = add_structure_features(df)

        daily = aggregate_daily(df, tf)
        daily_dfs.append(daily)
        
        print(f"[BTC CONTEXT] {tf} daily rows: {len(daily)}")

    context_df = daily_dfs[0]

    for other in daily_dfs[1:]:
        context_df = context_df.merge(other, on="date", how="outer")

    context_df = context_df.sort_values("date").reset_index(drop=True)
    context_df = context_df.tail(args.days).reset_index(drop=True)

    context_df = add_cross_tf_features(context_df)
    context_df = add_simple_regime_labels(context_df)

    trades_df = load_bot_trades(args.trades)
    context_df = add_bot_metrics(context_df, trades_df)

    output_path = Path(args.output)
    output_path.parent.mkdir(exist_ok=True)

    context_df.to_csv(output_path, index=False)

    print("")
    print("========================================")
    print(f"BTC Context Days exported:")
    print(output_path)
    print(f"Rows: {len(context_df)} final days")
    print(f"Warmup days used: {args.warmup_days}")
    print(f"Columns: {len(context_df.columns)}")
    print("========================================")


if __name__ == "__main__":
    main()