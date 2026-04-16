import sys
from pathlib import Path
import json

import pandas as pd
import streamlit as st
from streamlit_lightweight_charts import renderLightweightCharts

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT))

from data.market_data import fetch_history


# =========================
# CONFIG
# =========================
SYMBOL = "BTCUSDT"
TF = "15m"
DAYS = 30
ARG_TZ = "America/Argentina/Cordoba"

CHART_DATA_DIR = PROJECT_ROOT / "chart_data"


# =========================
# PAGE
# =========================
st.set_page_config(page_title="Trading Dashboard", layout="wide")
st.title("📈 Trading Dashboard")
st.caption("TradingView Lightweight Charts + entry events del replay/live pipeline")


# =========================
# HELPERS
# =========================
@st.cache_data(ttl=30)
def load_candles(symbol: str, tf: str, days: int) -> pd.DataFrame:
    df = fetch_history(symbol, tf, days).copy()
    if df.empty:
        return df

    if pd.api.types.is_numeric_dtype(df["timestamp"]):
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    else:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    df["timestamp"] = df["timestamp"].dt.tz_convert(ARG_TZ)

    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["timestamp", "open", "high", "low", "close"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


@st.cache_data(ttl=5)
def load_chart_events(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()

    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    for col in ["timestamp", "signal_timestamp"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], unit="ms", utc=True).dt.tz_convert(ARG_TZ)

    for col in ["signal_price", "entry_price", "tp", "sl", "atr"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def sanitize_candles(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()
    df = df.dropna(subset=["timestamp", "open", "high", "low", "close"])
    df = df.sort_values("timestamp")
    df = df.drop_duplicates(subset=["timestamp"], keep="last")

    df = df[
        (df["high"] >= df["open"]) &
        (df["high"] >= df["close"]) &
        (df["low"] <= df["open"]) &
        (df["low"] <= df["close"]) &
        (df["high"] >= df["low"])
    ].copy()

    return df.reset_index(drop=True)


def sanitize_events(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()
    df = df.dropna(subset=["timestamp"])
    df = df.sort_values("timestamp")
    df = df.reset_index(drop=True)
    return df


def to_lwc_time(ts: pd.Timestamp) -> int:
    return int(ts.tz_convert("UTC").timestamp())


def candles_to_lwc_data(df: pd.DataFrame) -> list[dict]:
    out = []

    for _, row in df.iterrows():
        ts = row["timestamp"]
        o = row["open"]
        h = row["high"]
        l = row["low"]
        c = row["close"]

        if pd.isna(ts) or pd.isna(o) or pd.isna(h) or pd.isna(l) or pd.isna(c):
            continue

        out.append(
            {
                "time": to_lwc_time(ts),
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
            }
        )

    out.sort(key=lambda x: x["time"])

    deduped = []
    seen = set()
    for item in out:
        if item["time"] in seen:
            continue
        seen.add(item["time"])
        deduped.append(item)

    return deduped


def events_to_markers(df: pd.DataFrame) -> list[dict]:
    if df.empty or "side" not in df.columns:
        return []

    markers = []
    filtered = df[df["side"].isin(["LONG", "SHORT"])].copy()
    filtered = filtered.dropna(subset=["timestamp"]).sort_values("timestamp")

    for _, row in filtered.iterrows():
        side = row["side"]

        markers.append(
            {
                "time": to_lwc_time(row["timestamp"]),
                "position": "belowBar" if side == "LONG" else "aboveBar",
                "shape": "arrowUp" if side == "LONG" else "arrowDown",
                "color": "#26a69a" if side == "LONG" else "#ef5350",
            }
        )

    return markers


def build_chart_payload(candles_df: pd.DataFrame, events_df: pd.DataFrame, symbol: str) -> list[dict]:
    candles_data = candles_to_lwc_data(candles_df)
    markers = events_to_markers(events_df)

    chart_options = {
        "height": 700,
        "layout": {
            "background": {"type": "solid", "color": "#0f172a"},
            "textColor": "#cbd5e1",
        },
        "grid": {
            "vertLines": {"color": "rgba(148, 163, 184, 0.08)"},
            "horzLines": {"color": "rgba(148, 163, 184, 0.08)"},
        },
        "crosshair": {
            "mode": 1,
        },
        "rightPriceScale": {
            "borderColor": "rgba(148, 163, 184, 0.2)",
        },
        "timeScale": {
            "borderColor": "rgba(148, 163, 184, 0.2)",
            "timeVisible": True,
            "secondsVisible": False,
        },
        "watermark": {
            "visible": True,
            "fontSize": 42,
            "horzAlign": "center",
            "vertAlign": "center",
            "color": "rgba(148, 163, 184, 0.10)",
            "text": f"{symbol} {TF}",
        },
    }

    series = [
        {
            "type": "Candlestick",
            "data": candles_data,
            "markers": markers,
            "options": {
                "upColor": "#26a69a",
                "downColor": "#ef5350",
                "borderVisible": False,
                "wickUpColor": "#26a69a",
                "wickDownColor": "#ef5350",
            },
        }
    ]

    return [{"chart": chart_options, "series": series}]


# =========================
# SIDEBAR
# =========================
st.sidebar.header("Config")
symbol = st.sidebar.text_input("Symbol", value=SYMBOL).upper().strip()
days = st.sidebar.slider("Days", min_value=3, max_value=90, value=DAYS)
show_table = st.sidebar.checkbox("Mostrar tabla de eventos", value=True)

events_path = CHART_DATA_DIR / f"chart_events_{symbol}.jsonl"

st.sidebar.write(f"PROJECT_ROOT: {PROJECT_ROOT}")
st.sidebar.write(f"EVENTS_PATH: {events_path}")
st.sidebar.write(f"EXISTS: {events_path.exists()}")


# =========================
# LOAD DATA
# =========================
candles_df = load_candles(symbol, TF, days)
candles_df = sanitize_candles(candles_df)

events_df = load_chart_events(events_path)
events_df = sanitize_events(events_df)

if candles_df.empty:
    st.error("No se pudieron cargar candles históricas.")
    st.stop()

if not events_df.empty:
    min_ts = candles_df["timestamp"].min()
    max_ts = candles_df["timestamp"].max()
    events_df = events_df[(events_df["timestamp"] >= min_ts) & (events_df["timestamp"] <= max_ts)].copy()


# =========================
# DEBUG
# =========================
candles_data_preview = candles_to_lwc_data(candles_df)
markers_preview = events_to_markers(events_df)

st.sidebar.write(f"CANDLES CLEAN: {len(candles_df)}")
st.sidebar.write(f"CANDLES DATA: {len(candles_data_preview)}")
st.sidebar.write(f"MARKERS: {len(markers_preview)}")

if candles_data_preview:
    st.sidebar.write("FIRST CANDLE:", candles_data_preview[0])

if markers_preview:
    st.sidebar.write("FIRST MARKER:", markers_preview[0])


# =========================
# METRICS
# =========================
col1, col2, col3, col4 = st.columns(4)
col1.metric("Candles", len(candles_df))
col2.metric("Events", len(events_df))
col3.metric("LONG", int((events_df["side"] == "LONG").sum()) if not events_df.empty and "side" in events_df.columns else 0)
col4.metric("SHORT", int((events_df["side"] == "SHORT").sum()) if not events_df.empty and "side" in events_df.columns else 0)


# =========================
# CHART
# =========================
chart_payload = build_chart_payload(candles_df, events_df, symbol)
renderLightweightCharts(chart_payload, key=f"lwc_{symbol}_{days}")


# =========================
# EVENTS TABLE
# =========================
st.subheader("Eventos")

if events_df.empty:
    st.warning("No encontré eventos para ese símbolo/rango. Verificá la ruta y que el replay haya generado el archivo.")
else:
    display_df = events_df.copy()

    for col in ["timestamp", "signal_timestamp"]:
        if col in display_df.columns:
            display_df[col] = display_df[col].dt.strftime("%Y-%m-%d %H:%M:%S")

    preferred_cols = [
        "event_type",
        "timestamp",
        "signal_timestamp",
        "side",
        "signal_price",
        "entry_price",
        "tp",
        "sl",
        "atr",
        "tf",
    ]
    preferred_cols = [c for c in preferred_cols if c in display_df.columns]
    display_df = display_df[preferred_cols]

    if show_table:
        st.dataframe(display_df, use_container_width=True, hide_index=True)