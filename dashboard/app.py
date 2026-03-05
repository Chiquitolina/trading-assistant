import sys
from pathlib import Path
import pandas as pd
import streamlit as st

# =========================
# CONFIG
# =========================
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR))

from engine.backtest.metrics import calculate_metrics  # noqa

TRADES_FILE = BASE_DIR / "trades.csv"
TZ = "America/Argentina/Buenos_Aires"

st.set_page_config(
    page_title="Trade Journal",
    layout="wide"
)

st.title("📊 Trade Journal Dashboard")

# =========================
# LOAD DATA
# =========================
if not TRADES_FILE.exists():
    st.error("❌ No trades.csv found")
    st.stop()

df = pd.read_csv(TRADES_FILE)

if df.empty:
    st.info("📭 No trades yet")
    st.stop()

# =========================
# CALCULATE ENTRY DISTANCE
# =========================
if "signal_price" in df.columns and "entry" in df.columns:
    df["entry_distance_pct"] = (
        (df["entry"] - df["signal_price"]) / df["signal_price"] * 100
    ).round(5)
else:
    df["entry_distance_pct"] = 0

# =========================
# QUICK METRICS
# =========================
st.markdown("## Overview")

col1, col2, col3, col4, col5 = st.columns(5)

total_trades = len(df)
net_pnl = round(df["pnl"].sum(), 2)
winrate = round((df["pnl"] > 0).mean() * 100, 2) if len(df) else 0

col1.metric("Trades", total_trades)
col2.metric("Net PnL", net_pnl)
col3.metric("Winrate", f"{winrate}%")
col4.metric("Avg PnL", round(df["pnl"].mean(), 2) if len(df) else 0)
col5.metric("Best Trade", round(df["pnl"].max(), 2) if len(df) else 0)

# =========================
# FORMAT DATES
# =========================
for col in ["signal_ts", "entry_ts", "exit_ts"]:
    if col in df.columns:
        df[col] = (
            pd.to_datetime(df[col], utc=True, errors="coerce")
            .dt.tz_convert(TZ)
            .dt.strftime("%d-%m %H:%M")
        )

# =========================
# METRICS CALCULATION
# =========================
all_trades = df.to_dict(orient="records")
long_trades = df[df["side"] == "LONG"].to_dict(orient="records")
short_trades = df[df["side"] == "SHORT"].to_dict(orient="records")

metrics_all = calculate_metrics(all_trades)
metrics_long = calculate_metrics(long_trades)
metrics_short = calculate_metrics(short_trades)

def safe_metric(metrics_dict, key, is_percent=False):
    value = metrics_dict.get(key, 0)

    try:
        value = float(value)
    except:
        value = 0

    if is_percent:
        return f"{value:.2f}%"

    return round(value, 2)

# =========================
# STRATEGY PERFORMANCE
# =========================
st.markdown("---")
st.subheader("📊 Strategy Performance")

col_long, col_short = st.columns(2)

# LONG
with col_long:

    st.markdown("## 🟢 LONG")

    l1, l2, l3 = st.columns(3)
    l1.metric("Trades", safe_metric(metrics_long, "trades"))
    l2.metric("Winrate", safe_metric(metrics_long, "winrate", True))
    l3.metric("Net PnL", safe_metric(metrics_long, "net_pnl"))

    l4, l5 = st.columns(2)
    l4.metric("Avg Win", safe_metric(metrics_long, "avg_win"))
    l5.metric("Avg Loss", safe_metric(metrics_long, "avg_loss"))

    l6, l7 = st.columns(2)
    l6.metric("Expectancy", safe_metric(metrics_long, "expectancy"))
    l7.metric("Max Drawdown", safe_metric(metrics_long, "max_drawdown"))

# SHORT
with col_short:

    st.markdown("## 🔴 SHORT")

    s1, s2, s3 = st.columns(3)
    s1.metric("Trades", safe_metric(metrics_short, "trades"))
    s2.metric("Winrate", safe_metric(metrics_short, "winrate", True))
    s3.metric("Net PnL", safe_metric(metrics_short, "net_pnl"))

    s4, s5 = st.columns(2)
    s4.metric("Avg Win", safe_metric(metrics_short, "avg_win"))
    s5.metric("Avg Loss", safe_metric(metrics_short, "avg_loss"))

    s6, s7 = st.columns(2)
    s6.metric("Expectancy", safe_metric(metrics_short, "expectancy"))
    s7.metric("Max Drawdown", safe_metric(metrics_short, "max_drawdown"))

# =========================
# TRADES TABLE
# =========================
st.markdown("---")
st.subheader("📋 Trades")

table_df = df.sort_values("entry_ts").copy()

table_df["entry_distance_pct"] = table_df["entry_distance_pct"].map(
    lambda x: f"{x:.4f}%"
)

st.dataframe(
    table_df,
    use_container_width=True
)

# =========================
# EQUITY CURVE
# =========================
st.markdown("---")
st.subheader("📈 Equity Curve")

df_sorted = df.sort_values("entry_ts").copy()

df_sorted["equity"] = df_sorted["pnl"].cumsum()

st.line_chart(
    df_sorted.set_index("entry_ts")["equity"],
    use_container_width=True
)

# =========================
# SLIPPAGE GRAPH
# =========================
st.markdown("---")
st.subheader("📏 Entry Slippage (%)")

df_slip = df.sort_values("entry_ts").copy()

st.line_chart(
    df_slip.set_index("entry_ts")["entry_distance_pct"],
    use_container_width=True
)

# =========================
# SIGNAL vs ENTRY SCATTER
# =========================
st.markdown("---")
st.subheader("🎯 Signal vs Entry Price")

scatter_df = df[["signal_price", "entry"]].dropna()

st.scatter_chart(
    scatter_df,
    x="signal_price",
    y="entry"
)