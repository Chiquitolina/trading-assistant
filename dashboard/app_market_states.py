import sys
from pathlib import Path

import pandas as pd
import streamlit as st
from streamlit_lightweight_charts import renderLightweightCharts


# =========================
# CONFIG
# =========================
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR))

from data.market_data import fetch_history  # noqa
from config.strategies.v1 import BACKTEST  # noqa

SYMBOL = "BTCUSDT"
FETCH_SYMBOL = "BTC/USDT"

STATES_FILE = BASE_DIR / f"market_states_{SYMBOL}.csv"

st.set_page_config(
    page_title="Market State Chart",
    layout="wide"
)

st.title("📈 Market State Chart")

def add_momentum_stats(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["mom_fe_pct"] = pd.NA
    df["mom_ae_pct"] = pd.NA
    df["mom_window_bars"] = pd.NA

    idxs = df.index[df["momentum_5m"].notna()].tolist()

    for i, idx in enumerate(idxs):
        momentum = str(df.loc[idx, "momentum_5m"])
        entry = float(df.loc[idx, "close"])

        next_idx = idx + 20  # ventana fija (20 velas aprox)
        future = df.loc[idx: min(next_idx, len(df) - 1)]

        if len(future) < 2:
            continue

        max_price = future["high"].max()
        min_price = future["low"].min()

        # ======================
        # EXPECTATIVA DIRECCIONAL
        # ======================
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
# HELPERS
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

        # ======================
        # BULLISH
        # ======================
        if trend == "bullish":
            fe = (max_price - entry) / entry * 100
            ae = (entry - min_price) / entry * 100

        # ======================
        # BEARISH
        # ======================
        elif trend == "bearish":
            fe = (entry - min_price) / entry * 100
            ae = (max_price - entry) / entry * 100

        # ======================
        # NEUTRAL (RANGO)
        # ======================
        elif trend == "neutral":
            last_close = future.iloc[-1]["close"]

            # rango total del movimiento
            fe = (max_price - min_price) / entry * 100

            # cuánto se alejó el cierre final (clave)
            ae = abs(last_close - entry) / entry * 100

        else:
            continue

        df.loc[idx, "trend_fe_pct"] = round(fe, 3)
        df.loc[idx, "trend_ae_pct"] = round(ae, 3)
        df.loc[idx, "trend_window_bars"] = len(future)

    return df

def marker_time_for_chart(state_ts: pd.Timestamp, tf: str) -> int:
    if tf == "1h":
        mapped_ts = state_ts.floor("1h")
    else:
        mapped_ts = state_ts

    return int(mapped_ts.value // 10**9)


def add_marker(time, position, shape, color, text):
    markers.append({
        "time": int(time),
        "position": position,
        "shape": shape,
        "color": color,
        "text": text,
    })


def get_momentum_marker(momentum: str):
    momentum = str(momentum)

    if momentum == "breakout_up_strong":
        return {"label": "BO↑ STR", "color": "#00e676", "position": "belowBar"}

    if momentum == "breakout_up_weak":
        return {"label": "BO↑", "color": "#66bb6a", "position": "belowBar"}

    if momentum == "breakout_down_strong":
        return {"label": "BO↓ STR", "color": "#ff1744", "position": "aboveBar"}

    if momentum == "breakout_down_weak":
        return {"label": "BO↓", "color": "#ef5350", "position": "aboveBar"}

    if momentum == "bullish_pressure":
        return {"label": "PRESS↑", "color": "#2979ff", "position": "belowBar"}

    if momentum == "bearish_pressure":
        return {"label": "PRESS↓", "color": "#9c27b0", "position": "aboveBar"}

    if momentum == "trend_continuation_up":
        return {"label": "CONT↑", "color": "#00c853", "position": "belowBar"}

    if momentum == "trend_continuation_down":
        return {"label": "CONT↓", "color": "#d50000", "position": "aboveBar"}

    if momentum == "exhaustion_up":
        return {"label": "EXH↑", "color": "#ffb300", "position": "aboveBar"}

    if momentum == "exhaustion_down":
        return {"label": "EXH↓", "color": "#ffb300", "position": "belowBar"}

    if momentum == "inside_bullish_weak":
        return {"label": "INS↑", "color": "#90caf9", "position": "belowBar"}

    if momentum == "inside_bearish_weak":
        return {"label": "INS↓", "color": "#ce93d8", "position": "aboveBar"}

    if momentum == "inside_bar":
        return {"label": "INSIDE", "color": "#90a4ae", "position": "aboveBar"}

    if momentum == "indecision":
        return {"label": "IND", "color": "#b0bec5", "position": "aboveBar"}

    return {"label": "M?", "color": "#b0bec5", "position": "aboveBar"}


@st.cache_data(ttl=300)
def load_chart_data(symbol: str, tf: str, days: int):
    df = fetch_history(symbol, tf, days)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    df["timestamp"] = (
        df["timestamp"]
        .dt.tz_convert("America/Argentina/Cordoba")
        .dt.tz_localize(None)
    )

    return df.sort_values("timestamp").reset_index(drop=True)


# =========================
# LOAD MARKET STATES
# =========================
if not STATES_FILE.exists():
    st.error(f"No existe el archivo: {STATES_FILE}")
    st.stop()

df_states = pd.read_csv(STATES_FILE)

if df_states.empty:
    st.warning("El archivo market_states está vacío.")
    st.stop()

df_states["timestamp"] = pd.to_datetime(df_states["timestamp"])
df_states = df_states.sort_values("timestamp").reset_index(drop=True)
df_states = add_direction_stats(df_states)
df_states = add_trend_stats(df_states)
df_states = add_momentum_stats(df_states)

# =========================
# SIDEBAR CONTROLS
# =========================
st.sidebar.title("⚙️ Visualización")

chart_tf = st.sidebar.selectbox(
    "Chart timeframe",
    ["15m", "5m", "1h"],
    index=0
)

show_trend = st.sidebar.checkbox("Trend changes 1h", value=True)
show_direction = st.sidebar.checkbox("Direction changes 15m", value=True)
show_direction_stats = st.sidebar.checkbox(
    "Mostrar FE/AE en Direction",
    value=True
)
show_momentum = st.sidebar.checkbox("Momentum 5m", value=False)

selected_momentums = []

if show_momentum:
    st.sidebar.markdown("### Momentum filters")

    momentum_options = sorted(
        df_states["momentum_5m"]
        .dropna()
        .astype(str)
        .unique()
        .tolist()
    )

    default_momentums = [
        m for m in momentum_options
        if m in [
            "breakout_up_strong",
            "breakout_down_strong",
        ]
    ]

    selected_momentums = st.sidebar.multiselect(
        "Elegí momentums a mostrar",
        options=momentum_options,
        default=default_momentums
    )

st.sidebar.markdown("---")
st.sidebar.write("Tip: activá solo una capa a la vez para debuggear.")


# =========================
# LOAD CHART CANDLES
# =========================
days = BACKTEST["days"] + BACKTEST["warmup"]

if chart_tf == "15m":
    df_chart = df_states[["timestamp", "open", "high", "low", "close"]].copy()
else:
    df_chart = load_chart_data(FETCH_SYMBOL, chart_tf, days)

if df_chart.empty:
    st.warning("No hay velas para mostrar.")
    st.stop()

df_chart["time"] = df_chart["timestamp"].astype("int64") // 10**9
df_states["time"] = df_states["timestamp"].astype("int64") // 10**9


# =========================
# FILTER SAME RANGE
# =========================
min_ts = df_states["timestamp"].min()
max_ts = df_states["timestamp"].max()

df_chart = df_chart[
    (df_chart["timestamp"] >= min_ts) &
    (df_chart["timestamp"] <= max_ts)
].copy()

if df_chart.empty:
    st.warning("No hay velas dentro del rango de market_states.")
    st.stop()


# =========================
# CANDLES
# =========================
candles = []

for _, row in df_chart.iterrows():
    candles.append({
        "time": int(row["time"]),
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
    })


# =========================
# MARKERS
# =========================
markers = []

valid_chart_times = set(df_chart["time"].astype(int).tolist())

for _, row in df_states.iterrows():
    state_ts = row["timestamp"]
    time = marker_time_for_chart(state_ts, chart_tf)

    if time not in valid_chart_times:
        continue

    # ======================
    # TREND CHANGES
    # ======================
    if show_trend and bool(row["trend_changed"]):
        trend = row["trend_1h"]

        if trend == "bullish":
            add_marker(time, "belowBar", "arrowUp", "#26a69a", "Trend ↑")

        elif trend == "bearish":
            add_marker(time, "aboveBar", "arrowDown", "#ef5350", "Trend ↓")

        else:
            add_marker(time, "aboveBar", "circle", "#b0bec5", "Trend →")

    # ======================
    # DIRECTION CHANGES + STATS
    # ======================
    if show_direction and bool(row["direction_changed"]):
        direction = row["direction_15m"]

        fe = row.get("fe_pct")
        ae = row.get("ae_pct")

        stats_txt = ""
        if show_direction_stats and pd.notna(fe) and pd.notna(ae):
            stats_txt = f" FE {float(fe):.2f}% / AE {float(ae):.2f}%"

        if direction == "up":
            add_marker(
                time=time,
                position="belowBar",
                shape="circle",
                color="#00c853",
                text=f"Dir ↑{stats_txt}",
            )

        elif direction == "down":
            add_marker(
                time=time,
                position="aboveBar",
                shape="circle",
                color="#ff1744",
                text=f"Dir ↓{stats_txt}",
            )

        else:
            add_marker(
                time=time,
                position="aboveBar",
                shape="circle",
                color="#ffd54f",
                text=f"Dir →{stats_txt}",
            )

    # ======================
    # MOMENTUM STATES
    # ======================
    momentum = str(row["momentum_5m"])

    if show_momentum and momentum in selected_momentums:
        marker = get_momentum_marker(momentum)

        add_marker(
            time=time,
            position=marker["position"],
            shape="circle",
            color=marker["color"],
            text=marker["label"],
        )


# =========================
# REMOVE DUPLICATE MARKERS SAME TIME/TEXT
# =========================
unique_markers = []
seen = set()

for marker in markers:
    key = (marker["time"], marker["text"])
    if key not in seen:
        unique_markers.append(marker)
        seen.add(key)

markers = unique_markers


# =========================
# CHART OPTIONS
# =========================
chart_options = {
    "layout": {
        "background": {"type": "solid", "color": "#0e1117"},
        "textColor": "#d1d4dc",
    },
    "grid": {
        "vertLines": {"color": "#1f2937"},
        "horzLines": {"color": "#1f2937"},
    },
    "crosshair": {
        "mode": 1
    },
    "rightPriceScale": {
        "borderColor": "#485c7b",
    },
    "timeScale": {
        "borderColor": "#485c7b",
        "timeVisible": True,
        "secondsVisible": False,
    },
    "height": 650,
}


series = [
    {
        "type": "Candlestick",
        "data": candles,
        "options": {
            "upColor": "#26a69a",
            "downColor": "#ef5350",
            "borderVisible": False,
            "wickUpColor": "#26a69a",
            "wickDownColor": "#ef5350",
        },
        "markers": markers,
    }
]


# =========================
# RENDER
# =========================
st.caption(
    f"Chart TF: {chart_tf} | "
    f"Trend source: 1h | Direction source: 15m | Momentum source: 5m"
)

renderLightweightCharts([
    {
        "chart": chart_options,
        "series": series,
    }
], key=f"market_states_chart_{chart_tf}_{show_trend}_{show_direction}_{show_direction_stats}_{show_momentum}_{'_'.join(selected_momentums)}")


# =========================
# DIRECTION PERFORMANCE SUMMARY
# =========================
if show_direction:
    st.markdown("### 📊 Direction Performance")

    direction_changes = df_states[
        (df_states["direction_changed"] == True) &
        (df_states["direction_15m"].isin(["up", "down"])) &
        (df_states["fe_pct"].notna()) &
        (df_states["ae_pct"].notna())
    ].copy()

    if not direction_changes.empty:
        summary = (
            direction_changes
            .groupby("direction_15m")
            .agg(
                signals=("direction_15m", "count"),
                avg_fe_pct=("fe_pct", "mean"),
                avg_ae_pct=("ae_pct", "mean"),
                max_fe_pct=("fe_pct", "max"),
                max_ae_pct=("ae_pct", "max"),
                avg_window_bars=("direction_window_bars", "mean"),
            )
            .reset_index()
        )

        summary["avg_fe_pct"] = summary["avg_fe_pct"].astype(float).round(3)
        summary["avg_ae_pct"] = summary["avg_ae_pct"].astype(float).round(3)
        summary["max_fe_pct"] = summary["max_fe_pct"].astype(float).round(3)
        summary["max_ae_pct"] = summary["max_ae_pct"].astype(float).round(3)
        summary["avg_window_bars"] = summary["avg_window_bars"].astype(float).round(2)

        st.dataframe(summary, use_container_width=True)
    else:
        st.info("No hay suficientes cambios de direction para calcular estadísticas.")
        
if show_momentum:
    st.markdown("### 📊 Momentum Performance")

    mom_changes = df_states[
        df_states["mom_fe_pct"].notna()
    ].copy()

    if not mom_changes.empty:
        summary = (
            mom_changes
            .groupby("momentum_5m")
            .agg(
                signals=("momentum_5m", "count"),
                avg_fe=("mom_fe_pct", "mean"),
                avg_ae=("mom_ae_pct", "mean"),
                max_fe=("mom_fe_pct", "max"),
                max_ae=("mom_ae_pct", "max"),
            )
            .reset_index()
        )

        # opcional: formatear
        summary["avg_fe"] = summary["avg_fe"].round(3)
        summary["avg_ae"] = summary["avg_ae"].round(3)

        st.dataframe(summary, use_container_width=True)
    else:
        st.info("No hay datos de momentum para mostrar.")
        
# =========================
# TREND PERFORMANCE SUMMARY
# =========================
if show_trend:
    st.markdown("### 📊 Trend Performance")

    trend_changes = df_states[
        (df_states["trend_changed"] == True) &
        (df_states["trend_1h"].isin(["bullish", "bearish", "neutral"])) &
        (df_states["trend_fe_pct"].notna()) &
        (df_states["trend_ae_pct"].notna())
    ].copy()

    if not trend_changes.empty:
        summary = (
            trend_changes
            .groupby("trend_1h")
            .agg(
                signals=("trend_1h", "count"),
                avg_fe_pct=("trend_fe_pct", "mean"),
                avg_ae_pct=("trend_ae_pct", "mean"),
                max_fe_pct=("trend_fe_pct", "max"),
                max_ae_pct=("trend_ae_pct", "max"),
                avg_window_bars=("trend_window_bars", "mean"),
            )
            .reset_index()
        )

        summary["avg_fe_pct"] = summary["avg_fe_pct"].astype(float).round(3)
        summary["avg_ae_pct"] = summary["avg_ae_pct"].astype(float).round(3)
        summary["max_fe_pct"] = summary["max_fe_pct"].astype(float).round(3)
        summary["max_ae_pct"] = summary["max_ae_pct"].astype(float).round(3)
        summary["avg_window_bars"] = summary["avg_window_bars"].astype(float).round(2)

        st.caption(
            "Para bullish/bearish: FE = movimiento a favor, AE = movimiento en contra. "
            "Para neutral: FE = rango total, AE = desplazamiento del cierre."
        )

        st.dataframe(summary, use_container_width=True)
    else:
        st.info("No hay suficientes cambios de trend para calcular estadísticas.")

# =========================
# MOMENTUM LEGEND
# =========================
if show_momentum:
    st.markdown("### 🧠 Momentum Legend")

    st.markdown("""
| Insignia | Significado |
|---|---|
| BO↑ STR | Breakout alcista fuerte |
| BO↑ | Breakout alcista débil |
| BO↓ STR | Breakout bajista fuerte |
| BO↓ | Breakout bajista débil |
| PRESS↑ | Presión compradora |
| PRESS↓ | Presión vendedora |
| CONT↑ | Continuación alcista |
| CONT↓ | Continuación bajista |
| EXH↑ | Agotamiento alcista |
| EXH↓ | Agotamiento bajista |
| INS↑ | Inside bullish débil |
| INS↓ | Inside bearish débil |
| INSIDE | Inside bar / compresión |
| IND | Indecisión |
""")
    
    st.markdown("### 📊 Combined Signal Performance")

combo = df_states[
    (df_states["mom_fe_pct"].notna()) &
    (df_states["trend_1h"].notna()) &
    (df_states["direction_15m"].notna())
].copy()

summary = (
    combo
    .groupby(["trend_1h", "direction_15m", "momentum_5m"])
    .agg(
        signals=("momentum_5m", "count"),
        avg_fe=("mom_fe_pct", "mean"),
        avg_ae=("mom_ae_pct", "mean"),
    )
    .reset_index()
)

summary["edge"] = summary["avg_fe"] - summary["avg_ae"]

# opcional: filtrar ruido
summary = summary[summary["signals"] > 30]

# ordenar por edge
summary = summary.sort_values("edge", ascending=False)

st.dataframe(summary, use_container_width=True)


# =========================
# DEBUG TABLE
# =========================
st.subheader("Últimos cambios detectados")

changes = df_states[
    (df_states["trend_changed"] == True) |
    (df_states["direction_changed"] == True)
].copy()

st.dataframe(
    changes[[
        "timestamp",
        "close",
        "trend_1h",
        "direction_15m",
        "momentum_5m",
        "trend_changed",
        "direction_changed",
        "fe_pct",
        "ae_pct",
        "direction_window_bars",
    ]].tail(80),
    use_container_width=True
)