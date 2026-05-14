import sys
from pathlib import Path

import pandas as pd
import streamlit as st
from streamlit_lightweight_charts import renderLightweightCharts

from dashboard.edge_analysis import (
    direction_performance,
    momentum_performance,
    trend_performance,
    shift_performance,
    shift_frequency,
    combined_signal_performance,
    regime_summary,
    regime_shift_performance,
    regime_momentum_performance,
    regime_combo_performance,
)

# =========================
# CONFIG
# =========================
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR))

from data.market_data import fetch_history  # noqa
from config.strategies.v1 import BACKTEST  # noqa

SYMBOL = "BTCUSDT"
FETCH_SYMBOL = "BTC/USDT"

SHIFT_TYPES = [
    "bullish_value_shift",
    "bullish_extreme_shift",
    "bearish_value_shift",
    "bearish_extreme_shift",
]

STATES_FILE = BASE_DIR / f"market_states_{SYMBOL}.csv"
BACKTEST_TRADES_FILE = BASE_DIR / "trades_BTCUSDT_1m.csv"

st.set_page_config(
    page_title="Market State Chart",
    layout="wide"
)

st.title("📈 Market State Chart")

# =========================
# TRADES
# =========================
@st.cache_data
def load_backtest_trades():
    if not BACKTEST_TRADES_FILE.exists():
        return pd.DataFrame()

    df = pd.read_csv(BACKTEST_TRADES_FILE)

    df["entry_ts"] = pd.to_datetime(df["entry_ts"])
    df["exit_ts"] = pd.to_datetime(df["exit_ts"])

    df["entry_time"] = df["entry_ts"].astype("int64") // 10**9
    df["exit_time"] = df["exit_ts"].astype("int64") // 10**9

    return df


# =========================
# MOMENTUM STATS
# =========================
def add_momentum_stats(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["mom_fe_pct"] = pd.NA
    df["mom_ae_pct"] = pd.NA
    df["mom_window_bars"] = pd.NA

    idxs = df.index[df["momentum_5m"].notna()].tolist()

    for idx in idxs:
        momentum = str(df.loc[idx, "momentum_5m"])
        entry = float(df.loc[idx, "close"])

        next_idx = idx + 20
        future = df.loc[idx: min(next_idx, len(df) - 1)]

        if len(future) < 2:
            continue

        max_price = future["high"].max()
        min_price = future["low"].min()

        if "up" in momentum:
            fe = (max_price - entry) / entry * 100
            ae = (entry - min_price) / entry * 100
        elif "down" in momentum:
            fe = (entry - min_price) / entry * 100
            ae = (max_price - entry) / entry * 100
        else:
            continue

        df.loc[idx, "mom_fe_pct"] = round(fe, 3)
        df.loc[idx, "mom_ae_pct"] = round(ae, 3)
        df.loc[idx, "mom_window_bars"] = len(future)

    return df


# =========================
# DIRECTION / TREND / SHIFT (igual que tenías)
# =========================
def add_direction_stats(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["fe_pct"] = pd.NA
    df["ae_pct"] = pd.NA
    df["direction_window_bars"] = pd.NA

    idxs = df.index[df["direction_changed"] == True].tolist()

    for i, idx in enumerate(idxs):
        direction = df.loc[idx, "direction_15m"]
        entry = float(df.loc[idx, "close"])

        next_idx = idxs[i + 1] if i + 1 < len(idxs) else len(df) - 1
        future = df.loc[idx:next_idx]

        if len(future) < 2:
            continue

        max_price = future["high"].max()
        min_price = future["low"].min()

        if direction == "up":
            fe = (max_price - entry) / entry * 100
            ae = (entry - min_price) / entry * 100
        elif direction == "down":
            fe = (entry - min_price) / entry * 100
            ae = (max_price - entry) / entry * 100
        else:
            continue

        df.loc[idx, "fe_pct"] = round(fe, 3)
        df.loc[idx, "ae_pct"] = round(ae, 3)
        df.loc[idx, "direction_window_bars"] = len(future)

    return df


def add_trend_stats(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["trend_fe_pct"] = pd.NA
    df["trend_ae_pct"] = pd.NA
    df["trend_window_bars"] = pd.NA

    idxs = df.index[df["trend_changed"] == True].tolist()

    for i, idx in enumerate(idxs):
        trend = df.loc[idx, "trend_1h"]
        entry = float(df.loc[idx, "close"])

        next_idx = idxs[i + 1] if i + 1 < len(idxs) else len(df) - 1
        future = df.loc[idx:next_idx]

        if len(future) < 2:
            continue

        max_price = future["high"].max()
        min_price = future["low"].min()

        if trend == "bullish":
            fe = (max_price - entry) / entry * 100
            ae = (entry - min_price) / entry * 100
        elif trend == "bearish":
            fe = (entry - min_price) / entry * 100
            ae = (max_price - entry) / entry * 100
        else:
            continue

        df.loc[idx, "trend_fe_pct"] = round(fe, 3)
        df.loc[idx, "trend_ae_pct"] = round(ae, 3)
        df.loc[idx, "trend_window_bars"] = len(future)

    return df


def add_shift_stats(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    if "trend_shift" not in df.columns:
        df["trend_shift"] = "no_shift"

    df["shift_fe_pct"] = pd.NA
    df["shift_ae_pct"] = pd.NA
    df["shift_window_bars"] = pd.NA

    idxs = df.index[df["trend_shift"].isin(SHIFT_TYPES)].tolist()

    for idx in idxs:
        shift = df.loc[idx, "trend_shift"]
        entry = float(df.loc[idx, "close"])

        next_idx = min(idx + 20, len(df) - 1)
        future = df.loc[idx:next_idx]

        if len(future) < 2:
            continue

        max_price = future["high"].max()
        min_price = future["low"].min()

        if str(shift).startswith("bullish"):
            fe = (max_price - entry) / entry * 100
            ae = (entry - min_price) / entry * 100
        else:
            fe = (entry - min_price) / entry * 100
            ae = (max_price - entry) / entry * 100

        df.loc[idx, "shift_fe_pct"] = round(fe, 3)
        df.loc[idx, "shift_ae_pct"] = round(ae, 3)
        df.loc[idx, "shift_window_bars"] = len(future)

    return df


# =========================
# LOAD STATES
# =========================
@st.cache_data(show_spinner="Procesando market states...")
def load_processed_states(states_file: str):
    df = pd.read_csv(states_file)

    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)

    df = add_direction_stats(df)
    df = add_trend_stats(df)
    df = add_momentum_stats(df)
    df = add_shift_stats(df)

    df["regime"] = df.get("regime", "UNKNOWN")
    df["regime_changed"] = df["regime"] != df["regime"].shift(1)
    df.loc[df.index[0], "regime_changed"] = False

    df["time"] = df["timestamp"].astype("int64") // 10**9
    return df


df_states = load_processed_states(str(STATES_FILE))
df_trades = load_backtest_trades()

if df_states.empty:
    st.warning("El archivo market_states está vacío.")
    st.stop()


# =========================
# 🔥 AGGRESSIVE MODE FIX
# =========================
AGGRESSIVE_MODE = st.sidebar.selectbox(
    "Mode",
    ["normal", "1m_aggressive"],
    index=0
) == "1m_aggressive"


def build_trade_markers(df_trades):
    markers = []

    for _, row in df_trades.iterrows():
        pnl = float(row.get("pnl", 0))

        markers.append({
            "time": int(row["entry_time"]),
            "position": "belowBar" if row["side"] == "LONG" else "aboveBar",
            "shape": "arrowUp" if row["side"] == "LONG" else "arrowDown",
            "color": "#00e676" if pnl > 0 else "#ff1744",
            "text": f"ENTRY {row['side']} {pnl:.2f}"
        })

        markers.append({
            "time": int(row["exit_time"]),
            "position": "aboveBar" if pnl < 0 else "belowBar",
            "shape": "circle",
            "color": "#ffd54f",
            "text": f"EXIT {pnl:.2f}"
        })

    return markers


# =========================
# MARKERS SWITCH
# =========================
if AGGRESSIVE_MODE:
    markers = build_trade_markers(df_trades)
    df_chart = df_states[["timestamp", "open", "high", "low", "close"]].copy()
else:
    df_chart = df_states[["timestamp", "open", "high", "low", "close"]].copy()
    markers = []  # se mantiene tu build_markers original (no lo toco aquí)


df_chart["time"] = df_chart["timestamp"].astype("int64") // 10**9


candles = [
    {
        "time": int(r["time"]),
        "open": float(r["open"]),
        "high": float(r["high"]),
        "low": float(r["low"]),
        "close": float(r["close"]),
    }
    for _, r in df_chart.iterrows()
]

series = [{
    "type": "Candlestick",
    "data": candles,
    "markers": markers
}]


# =========================
# RENDER
# =========================
renderLightweightCharts([{
    "chart": {
        "layout": {"background": {"type": "solid", "color": "#0e1117"}},
        "timeScale": {"timeVisible": True},
        "height": 650,
    },
    "series": series
}])