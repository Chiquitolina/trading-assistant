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

        elif str(shift).startswith("bearish"):
            fe = (entry - min_price) / entry * 100
            ae = (max_price - entry) / entry * 100

        else:
            continue

        df.loc[idx, "shift_fe_pct"] = round(fe, 3)
        df.loc[idx, "shift_ae_pct"] = round(ae, 3)
        df.loc[idx, "shift_window_bars"] = len(future)

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

def get_regime_marker(regime: str):
    regime = str(regime)

    if regime == "SELL_RIPS":
        return {"label": "SELL RIPS", "color": "#ff1744", "position": "aboveBar"}

    if regime == "BUY_DIPS":
        return {"label": "BUY DIPS", "color": "#00e676", "position": "belowBar"}

    if regime == "CHOP":
        return {"label": "CHOP", "color": "#ffd54f", "position": "aboveBar"}

    if regime == "MIXED":
        return {"label": "MIXED", "color": "#b0bec5", "position": "aboveBar"}

    return {"label": "UNKNOWN", "color": "#78909c", "position": "aboveBar"}

CHART_CACHE_DIR = BASE_DIR / "data" / "chart_cache"

@st.cache_data(show_spinner="Cargando velas locales...")
def load_chart_data_from_csv(tf: str):
    path = CHART_CACHE_DIR / f"{SYMBOL}_{tf}.csv"

    if not path.exists():
        return pd.DataFrame()

    df = pd.read_csv(path)

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

@st.cache_data(show_spinner="Procesando market states...")
def load_processed_states(states_file: str):
    df = pd.read_csv(states_file)

    if df.empty:
        return df

    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)

    df = add_direction_stats(df)
    df = add_trend_stats(df)
    df = add_momentum_stats(df)
    df = add_shift_stats(df)

    if "regime" not in df.columns:
        df["regime"] = "UNKNOWN"

    df["regime_changed"] = df["regime"] != df["regime"].shift(1)
    df.loc[df.index[0], "regime_changed"] = False

    df["time"] = df["timestamp"].astype("int64") // 10**9

    return df

df_states = load_processed_states(str(STATES_FILE))

if df_states.empty:
    st.warning("El archivo market_states está vacío.")
    st.stop()

if "regime" not in df_states.columns:
    df_states["regime"] = "UNKNOWN"

df_states["regime_changed"] = df_states["regime"] != df_states["regime"].shift(1)
df_states.loc[df_states.index[0], "regime_changed"] = False

# =========================
# SIDEBAR CONTROLS
# =========================
st.sidebar.title("⚙️ Visualización")

chart_tf = st.sidebar.selectbox(
    "Chart timeframe",
    ["15m", "5m", "1h", "4h", "1d"],
    index=0
)

show_trend = st.sidebar.checkbox("Trend changes 1h", value=False)
show_shift = st.sidebar.checkbox("Trend Shift", value=False)
show_regime = st.sidebar.checkbox("Regime changes", value=True)
show_direction = st.sidebar.checkbox("Direction changes 15m", value=False)
show_direction_stats = st.sidebar.checkbox(
    "Mostrar FE/AE en Direction",
    value=False
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
# EMA OPTIONS
# =========================
show_ema20 = st.sidebar.checkbox("EMA 20", value=False)
show_ema50 = st.sidebar.checkbox("EMA 50", value=True)


# =========================
# LOAD CHART CANDLES
# =========================
days = BACKTEST["days"] + BACKTEST["warmup"]

if chart_tf == "15m":
    df_chart = df_states[["timestamp", "open", "high", "low", "close"]].copy()
else:
    df_chart = load_chart_data_from_csv(chart_tf)
    
if df_chart.empty:
    st.warning("No hay velas para mostrar.")
    st.stop()

df_chart["time"] = df_chart["timestamp"].astype("int64") // 10**9
df_states["time"] = df_states["timestamp"].astype("int64") // 10**9

from ta.trend import EMAIndicator

if show_ema20:
    df_chart["ema20"] = EMAIndicator(df_chart["close"], window=20).ema_indicator()

if show_ema50:
    df_chart["ema50"] = EMAIndicator(df_chart["close"], window=50).ema_indicator()

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


@st.cache_data(show_spinner="Construyendo markers...")
def build_markers(
    df_states,
    valid_chart_times,
    chart_tf,
    show_trend,
    show_shift,
    show_regime,
    show_direction,
    show_direction_stats,
    show_momentum,
    selected_momentums,
):
    markers = []

    def add_marker(time, position, shape, color, text):
        markers.append({
            "time": int(time),
            "position": position,
            "shape": shape,
            "color": color,
            "text": text,
        })

    valid_chart_times = set(valid_chart_times)
    selected_momentums = set(selected_momentums)

    for _, row in df_states.iterrows():
        state_ts = row["timestamp"]
        time = marker_time_for_chart(state_ts, chart_tf)

        if time not in valid_chart_times:
            continue

        if show_trend and bool(row["trend_changed"]):
            trend = row["trend_1h"]

            if trend == "bullish":
                add_marker(time, "belowBar", "arrowUp", "#26a69a", "Trend ↑")
            elif trend == "bearish":
                add_marker(time, "aboveBar", "arrowDown", "#ef5350", "Trend ↓")
            else:
                add_marker(time, "aboveBar", "circle", "#b0bec5", "Trend →")

        if show_shift and row.get("trend_shift", "no_shift") != "no_shift":
            shift = row["trend_shift"]

            if str(shift).startswith("bullish"):
                label = "E SHIFT ↑" if "extreme" in shift else "SHIFT ↑"
                add_marker(row["time"], "belowBar", "arrowUp", "green", label)

            elif str(shift).startswith("bearish"):
                label = "E SHIFT ↓" if "extreme" in shift else "SHIFT ↓"
                add_marker(row["time"], "aboveBar", "arrowDown", "red", label)

        if show_regime and bool(row.get("regime_changed", False)):
            regime = row.get("regime", "UNKNOWN")

            if regime != "UNKNOWN":
                marker = get_regime_marker(regime)
                add_marker(
                    time,
                    marker["position"],
                    "circle",
                    marker["color"],
                    marker["label"],
                )

        if show_direction and bool(row["direction_changed"]):
            direction = row["direction_15m"]

            fe = row.get("fe_pct")
            ae = row.get("ae_pct")

            stats_txt = ""
            if show_direction_stats and pd.notna(fe) and pd.notna(ae):
                stats_txt = f" FE {float(fe):.2f}% / AE {float(ae):.2f}%"

            if direction == "up":
                add_marker(time, "belowBar", "circle", "#00c853", f"Dir ↑{stats_txt}")
            elif direction == "down":
                add_marker(time, "aboveBar", "circle", "#ff1744", f"Dir ↓{stats_txt}")
            else:
                add_marker(time, "aboveBar", "circle", "#ffd54f", f"Dir →{stats_txt}")

        momentum = str(row["momentum_5m"])

        if show_momentum and momentum in selected_momentums:
            marker = get_momentum_marker(momentum)
            add_marker(
                time,
                marker["position"],
                "circle",
                marker["color"],
                marker["label"],
            )

    unique_markers = []
    seen = set()

    for marker in markers:
        key = (marker["time"], marker["text"])
        if key not in seen:
            unique_markers.append(marker)
            seen.add(key)

    return unique_markers

markers = build_markers(
    df_states=df_states,
    valid_chart_times=df_chart["time"].astype(int).tolist(),
    chart_tf=chart_tf,
    show_trend=show_trend,
    show_shift=show_shift,
    show_regime=show_regime,
    show_direction=show_direction,
    show_direction_stats=show_direction_stats,
    show_momentum=show_momentum,
    selected_momentums=selected_momentums,
)

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
# EMA SERIES
# =========================

if show_ema20:
    ema20_data = [
        {"time": int(row["time"]), "value": float(row["ema20"])}
        for _, row in df_chart.iterrows()
        if pd.notna(row.get("ema20"))
    ]

    series.append({
        "type": "Line",
        "data": ema20_data,
        "options": {
            "color": "#00e5ff",
            "lineWidth": 1,
        },
    })

if show_ema50:
    ema50_data = [
        {"time": int(row["time"]), "value": float(row["ema50"])}
        for _, row in df_chart.iterrows()
        if pd.notna(row.get("ema50"))
    ]

    series.append({
        "type": "Line",
        "data": ema50_data,
        "options": {
            "color": "#ff9800",
            "lineWidth": 2,
        },
    })


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
], key=f"market_states_chart_{chart_tf}_{show_trend}_{show_shift}_{show_regime}_{show_direction}_{show_direction_stats}_{show_momentum}_{show_ema20}_{show_ema50}_{'_'.join(selected_momentums)}")


# =========================
# DIRECTION PERFORMANCE SUMMARY
# =========================
if show_direction:
    st.markdown("### 📊 Direction Performance")

    summary = direction_performance(df_states)

    if not summary.empty:
        st.dataframe(summary, use_container_width=True)
    else:
        st.info("No hay suficientes cambios de direction para calcular estadísticas.")


# =========================
# MOMENTUM PERFORMANCE SUMMARY
# =========================
if show_momentum:
    st.markdown("### 📊 Momentum Performance")

    summary = momentum_performance(df_states)

    if not summary.empty:
        st.dataframe(summary, use_container_width=True)
    else:
        st.info("No hay datos de momentum para mostrar.")


# =========================
# TREND PERFORMANCE SUMMARY
# =========================
if show_trend:
    st.markdown("### 📊 Trend Performance")

    summary = trend_performance(df_states)

    if not summary.empty:
        st.caption(
            "Para bullish/bearish: FE = movimiento a favor, AE = movimiento en contra. "
            "Para neutral: FE = rango total, AE = desplazamiento del cierre."
        )

        st.dataframe(summary, use_container_width=True)
    else:
        st.info("No hay suficientes cambios de trend para calcular estadísticas.")


# =========================
# SHIFT PERFORMANCE SUMMARY
# =========================
if show_shift:
    st.markdown("### 📊 Trend Shift Performance")

    summary = shift_performance(df_states)

    if not summary.empty:
        st.dataframe(summary, use_container_width=True)
    else:
        st.info("No hay suficientes shifts para calcular estadísticas.")


# =========================
# SHIFT FREQUENCY ANALYSIS
# =========================
if show_shift:
    st.markdown("### ⏱️ Shift Frequency")

    summary = shift_frequency(df_states)

    if not summary.empty:
        st.dataframe(summary, use_container_width=True)

        st.caption(
            "Cuántas velas pasan entre shifts. "
            "Muy bajo = demasiados shifts (ruido). "
            "Muy alto = llega tarde."
        )
    else:
        st.info("No hay suficientes shifts para analizar frecuencia.")


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


# =========================
# COMBINED SIGNAL PERFORMANCE
# =========================
st.markdown("### 📊 Combined Signal Performance")

summary = combined_signal_performance(df_states, min_signals=30)

if not summary.empty:
    st.dataframe(summary, use_container_width=True)
else:
    st.info("No hay suficientes combinaciones para calcular edge.")


# =========================
# REGIME SUMMARY
# =========================
if show_regime:
    st.markdown("### 🧠 Regime Summary")

    summary = regime_summary(df_states)

    if not summary.empty:
        st.dataframe(summary, use_container_width=True)
    else:
        st.info("No hay datos de régimen para mostrar.")
        
# =========================
# REGIME EDGE TESTS
# =========================
if show_regime:
    st.markdown("### 🧪 Regime + Shift Edge")

    summary = regime_shift_performance(df_states)

    if not summary.empty:
        st.dataframe(summary, use_container_width=True)
    else:
        st.info("No hay suficientes shifts por régimen.")

    st.markdown("### 🧪 Regime + Momentum Edge")

    summary = regime_momentum_performance(df_states, min_signals=30)

    if not summary.empty:
        st.dataframe(summary, use_container_width=True)
    else:
        st.info("No hay suficiente momentum por régimen.")

    st.markdown("### 🧪 Regime + Combo Edge")

    summary = regime_combo_performance(df_states, min_signals=20)

    if not summary.empty:
        st.dataframe(summary, use_container_width=True)
    else:
        st.info("No hay suficientes combinaciones por régimen.")

# =========================
# DEBUG TABLE
# =========================
st.subheader("Últimos cambios detectados")

changes = df_states[
    (df_states["trend_changed"] == True) |
    (df_states["direction_changed"] == True) |
    (df_states["trend_shift"] != "no_shift")
].copy()

st.dataframe(
    changes[[
        "timestamp",
        "close",
        "trend_1h",
        "direction_15m",
        "momentum_5m",
        "regime",
        "trend_changed",
        "direction_changed",
        "trend_shift",
        "fe_pct",
        "ae_pct",
        "direction_window_bars",
        "shift_fe_pct",
        "shift_ae_pct",
        "shift_window_bars",
    ]].tail(80),
    use_container_width=True
)