import os
import sys
import json
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from streamlit_autorefresh import st_autorefresh

# =========================
# CONFIG
# =========================
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR))

load_dotenv(BASE_DIR / ".env")

from engine.backtest.metrics import calculate_metrics  # noqa

TRADES_FILE = BASE_DIR / "trades.csv"
STATUS_FILE = BASE_DIR / "status.json"
TZ = "America/Argentina/Buenos_Aires"
SYMBOL = "BTCUSDT"
STATUS_TTL_SECONDS = 10

st.set_page_config(
    page_title="Trade Journal",
    layout="wide"
)

st.title("📊 Trade Journal Dashboard")
st_autorefresh(interval=2000, key="dashboard_refresh")


# =========================
# HELPERS
# =========================
def safe_metric(metrics_dict, key, is_percent=False):
    value = metrics_dict.get(key, 0)

    try:
        value = float(value)
    except Exception:
        value = 0

    if is_percent:
        return f"{value:.2f}%"

    return round(value, 2)


def render_status_dot(label: str, is_online: bool):
    color = "#00c853" if is_online else "#ff5252"
    text = "ONLINE" if is_online else "OFFLINE"

    st.markdown(
        f"""
        <div style="
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 8px 12px;
            border: 1px solid rgba(128,128,128,0.25);
            border-radius: 10px;
            margin-bottom: 6px;
            min-height: 78px;
        ">
            <div style="
                width: 14px;
                height: 14px;
                border-radius: 50%;
                background-color: {color};
                box-shadow: 0 0 8px {color};
                flex-shrink: 0;
            "></div>
            <div>
                <div style="font-size: 0.85rem; opacity: 0.8;">{label}</div>
                <div style="font-weight: 700; font-size: 1rem;">{text}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )


def get_last_signal(df):
    if df.empty or "side" not in df.columns:
        return "N/A"

    valid = df.dropna(subset=["side"])
    if valid.empty:
        return "N/A"

    if "entry_ts_dt" in valid.columns:
        valid = valid.sort_values("entry_ts_dt")

    return valid.iloc[-1]["side"]


def get_today_pnl(df, tz_name):
    if df.empty or "exit_ts" not in df.columns or "pnl" not in df.columns:
        return 0.0

    exit_dt = pd.to_datetime(df["exit_ts"], utc=True, errors="coerce")
    exit_dt = exit_dt.dt.tz_convert(tz_name)

    today = pd.Timestamp.now(tz=tz_name).date()
    mask = exit_dt.dt.date == today

    return round(df.loc[mask, "pnl"].fillna(0).sum(), 2)


def get_default_status():
    return {
        "engine_online": False,
        "ws_online": False,
        "symbol": SYMBOL,
        "balance": 0.0,
        "position_side": "NONE",
        "position_qty": 0.0,
        "entry_price": 0.0,
        "unpnl": 0.0,
        "last_signal": "N/A",
        "updated_at": None,
        "is_open": False,
        "is_stale": True,
        "error": None,
    }


def load_status():
    status = get_default_status()

    if not STATUS_FILE.exists():
        status["error"] = f"status.json not found: {STATUS_FILE}"
        return status

    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)

        updated_at_raw = raw.get("updated_at")
        updated_at = pd.to_datetime(updated_at_raw, errors="coerce")

        is_stale = True
        if pd.notnull(updated_at):
            now = pd.Timestamp.now()
            if getattr(updated_at, "tzinfo", None) is not None:
                updated_at = updated_at.tz_localize(None)
            age_seconds = (now - updated_at).total_seconds()
            is_stale = age_seconds > STATUS_TTL_SECONDS

        position_side = raw.get("position_side", "NONE")
        position_qty = float(raw.get("position_qty", 0.0) or 0.0)

        status.update({
            "engine_online": bool(raw.get("engine_online", False)) and not is_stale,
            "ws_online": bool(raw.get("ws_online", False)) and not is_stale,
            "symbol": raw.get("symbol", SYMBOL),
            "balance": round(float(raw.get("balance", 0.0) or 0.0), 2),
            "position_side": position_side,
            "position_qty": position_qty,
            "entry_price": float(raw.get("entry_price", 0.0) or 0.0),
            "unpnl": float(raw.get("unpnl", 0.0) or 0.0),
            "last_signal": raw.get("last_signal", "N/A"),
            "updated_at": updated_at_raw,
            "is_open": position_side not in ("NONE", "ERROR") and position_qty > 0,
            "is_stale": is_stale,
            "error": None,
        })

        return status

    except Exception as e:
        status["error"] = str(e)
        return status


# =========================
# LOAD DATA
# =========================
if TRADES_FILE.exists():
    df = pd.read_csv(TRADES_FILE)
else:
    df = pd.DataFrame()

# =========================
# CLEAN NUMERIC COLUMNS
# =========================
numeric_cols = [
    "pnl",
    "signal_price",
    "entry",
    "real_entry",
    "exit",
    "real_exit",
    "tp",
    "sl",
]

for col in numeric_cols:
    if col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")

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
# RAW DF FOR CALCS / CHARTS
# =========================
df_raw = df.copy()

for col in ["signal_ts", "entry_ts", "exit_ts"]:
    if col in df_raw.columns:
        df_raw[f"{col}_dt"] = pd.to_datetime(df_raw[col], utc=True, errors="coerce")

        try:
            df_raw[f"{col}_dt"] = df_raw[f"{col}_dt"].dt.tz_convert(TZ)
        except Exception:
            pass

# =========================
# STATUS PANEL DATA
# =========================
status = load_status()

engine_online = status["engine_online"]
ws_online = status["ws_online"]
balance = status["balance"]
symbol_from_status = status["symbol"] or SYMBOL
position_side = status["position_side"]
position_qty = status["position_qty"]
entry_price = status["entry_price"]
unpnl = status["unpnl"]
last_signal = status["last_signal"]
updated_at = status["updated_at"]

if last_signal in (None, "", "N/A"):
    last_signal = get_last_signal(df_raw)

pnl_today = get_today_pnl(df_raw, TZ)

# =========================
# SYSTEM STATUS
# =========================
st.markdown("## 🧠 System Status")

with st.container(border=True):
    c1, c2, c3, c4, c5, c6 = st.columns(6)

    with c1:
        render_status_dot("ENGINE", engine_online)

    with c2:
        render_status_dot("WS", ws_online)

    c3.metric("POSITION", position_side)
    c4.metric("uPnL", round(unpnl, 2))
    c5.metric("BALANCE", f"{balance} USDT")
    c6.metric("PNL TODAY", pnl_today)

    c7, c8 = st.columns(2)
    c7.metric("LAST SIGNAL", last_signal)
    c8.metric("SYMBOL", symbol_from_status)

    if status["error"]:
        st.error(f"❌ Status file error: {status['error']}")
    elif status["is_stale"]:
        st.warning("⚠️ Bot heartbeat stale or stopped")

    if updated_at:
        st.caption(f"Last heartbeat: {updated_at}")

    if status["is_open"]:
        st.success(
            f"🟢 Open position on exchange | "
            f"{symbol_from_status} | "
            f"{position_side} | "
            f"Qty: {position_qty} | "
            f"Entry: {entry_price} | "
            f"uPnL: {round(unpnl, 2)}"
        )
    else:
        st.info("⚪ No open position on exchange")

# =========================
# NO TRADES YET
# =========================
if df_raw.empty:
    st.markdown("---")
    st.info("📭 No trades yet")
    st.stop()

# =========================
# QUICK METRICS
# =========================
st.markdown("## Overview")

col1, col2, col3, col4, col5 = st.columns(5)

total_trades = len(df_raw)
net_pnl = round(df_raw["pnl"].sum(), 2) if "pnl" in df_raw.columns else 0
winrate = round((df_raw["pnl"] > 0).mean() * 100, 2) if len(df_raw) and "pnl" in df_raw.columns else 0

col1.metric("Trades", total_trades)
col2.metric("Net PnL", net_pnl)
col3.metric("Winrate", f"{winrate}%")
col4.metric("Avg PnL", round(df_raw["pnl"].mean(), 2) if len(df_raw) and "pnl" in df_raw.columns else 0)
col5.metric("Best Trade", round(df_raw["pnl"].max(), 2) if len(df_raw) and "pnl" in df_raw.columns else 0)

# =========================
# METRICS CALCULATION
# =========================
all_trades = df_raw.to_dict(orient="records")
long_trades = df_raw[df_raw["side"] == "LONG"].to_dict(orient="records") if "side" in df_raw.columns else []
short_trades = df_raw[df_raw["side"] == "SHORT"].to_dict(orient="records") if "side" in df_raw.columns else []

metrics_all = calculate_metrics(all_trades)
metrics_long = calculate_metrics(long_trades)
metrics_short = calculate_metrics(short_trades)

# =========================
# STRATEGY PERFORMANCE
# =========================
st.markdown("---")
st.subheader("📊 Strategy Performance")

col_long, col_short = st.columns(2)

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
# FORMAT DATES FOR DISPLAY ONLY
# =========================
df_display = df_raw.copy()

for col in ["signal_ts", "entry_ts", "exit_ts"]:
    dt_col = f"{col}_dt"

    if dt_col in df_display.columns:
        df_display[col] = df_display[dt_col].dt.strftime("%d-%m %H:%M")

# =========================
# TRADES TABLE
# =========================
st.markdown("---")
st.subheader("📋 Trades")

table_df = df_display.copy()

if "entry_ts_dt" in table_df.columns:
    table_df = table_df.sort_values("entry_ts_dt")

if "entry_distance_pct" in table_df.columns:
    table_df["entry_distance_pct"] = table_df["entry_distance_pct"].map(
        lambda x: f"{x:.4f}%" if pd.notnull(x) else "0.0000%"
    )

drop_cols = [c for c in ["signal_ts_dt", "entry_ts_dt", "exit_ts_dt"] if c in table_df.columns]
table_df = table_df.drop(columns=drop_cols)

st.dataframe(
    table_df,
    use_container_width=True
)

# =========================
# EQUITY CURVE
# =========================
st.markdown("---")
st.subheader("📈 Equity Curve")

df_equity = df_raw.copy()

if "entry_ts_dt" in df_equity.columns and "pnl" in df_equity.columns:
    df_equity = df_equity.sort_values("entry_ts_dt")
    df_equity["equity"] = df_equity["pnl"].fillna(0).cumsum()

    st.line_chart(
        df_equity.set_index("entry_ts_dt")["equity"],
        use_container_width=True
    )

# =========================
# SLIPPAGE GRAPH
# =========================
st.markdown("---")
st.subheader("📏 Entry Slippage (%)")

df_slip = df_raw.copy()

if "entry_ts_dt" in df_slip.columns and "entry_distance_pct" in df_slip.columns:
    df_slip = df_slip.sort_values("entry_ts_dt")

    st.line_chart(
        df_slip.set_index("entry_ts_dt")["entry_distance_pct"],
        use_container_width=True
    )

# =========================
# SIGNAL vs ENTRY SCATTER
# =========================
st.markdown("---")
st.subheader("🎯 Signal vs Entry Price")

if "signal_price" in df_raw.columns and "entry" in df_raw.columns:
    scatter_df = df_raw[["signal_price", "entry"]].dropna()

    if not scatter_df.empty:
        st.scatter_chart(
            scatter_df,
            x="signal_price",
            y="entry"
        )