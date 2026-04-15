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
CANDLE_MINUTES = 15

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


def render_signal_text(signal: str, trend: str, direction: str, momentum: str):
    signal = str(signal or "N/A").upper().strip()

    if signal == "LONG":
        color = "#00c853"
    elif signal == "SHORT":
        color = "#ff5252"
    else:
        color = "#b0bec5"

    trend = trend if trend not in (None, "", "N/A") else "-"
    direction = direction if direction not in (None, "", "N/A") else "-"
    momentum = momentum if momentum not in (None, "", "N/A") else "-"

    st.markdown(
        f"""
        <div style="line-height: 1.1;">
            <div style="font-size: 0.90rem; opacity: 1; color: white">
                LAST SIGNAL
            </div>
            <div style="font-size: 2.25rem; font-weight: 800; color: {color}; margin-top: 8px;">
                {signal}
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )

    st.caption(f"{trend} / {direction} / {momentum}")


def render_plan_text(status: str, reason: str, side: str, entry, tp, sl):
    status = str(status or "N/A").upper().strip()
    reason = reason if reason not in (None, "", "N/A") else "-"
    side = side if side not in (None, "", "N/A") else "-"

    if status == "EXECUTED":
        color = "#00c853"
    elif status == "DISCARDED":
        color = "#ff5252"
    elif status == "READY":
        color = "#ffd54f"
    elif status == "SKIPPED":
        color = "#90caf9"
    else:
        color = "#b0bec5"

    def fmt_price(x):
        if x in (None, "", "N/A"):
            return "-"
        try:
            return f"{float(x):.2f}"
        except Exception:
            return str(x)

    st.markdown(
        f"""
        <div style="line-height: 1.1;">
            <div style="font-size: 0.90rem; opacity: 1; color: white">
                LAST PLAN
            </div>
            <div style="font-size: 2.25rem; font-weight: 800; color: {color}; margin-top: 8px;">
                {status}
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )
    
    st.caption(reason)

    st.caption(
        f"{side} | entry: {fmt_price(entry)} | tp: {fmt_price(tp)} | sl: {fmt_price(sl)}"
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
        "signal_trend": "N/A",
        "signal_direction": "N/A",
        "signal_momentum": "N/A",

        "last_plan_status": "N/A",
        "last_plan_reason": "N/A",
        "last_plan_side": "N/A",
        "last_plan_entry": None,
        "last_plan_tp": None,
        "last_plan_sl": None,

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

            "last_signal": str(raw.get("last_signal", "N/A") or "N/A").upper(),
            "signal_trend": raw.get("signal_trend", "N/A"),
            "signal_direction": raw.get("signal_direction", "N/A"),
            "signal_momentum": raw.get("signal_momentum", "N/A"),

            "last_plan_status": raw.get("last_plan_status", "N/A"),
            "last_plan_reason": raw.get("last_plan_reason", "N/A"),
            "last_plan_side": raw.get("last_plan_side", "N/A"),
            "last_plan_entry": raw.get("last_plan_entry"),
            "last_plan_tp": raw.get("last_plan_tp"),
            "last_plan_sl": raw.get("last_plan_sl"),

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
# TRADE DURATION
# =========================
if "entry_ts_dt" in df_raw.columns and "exit_ts_dt" in df_raw.columns:
    duration_minutes = (
        (df_raw["exit_ts_dt"] - df_raw["entry_ts_dt"]).dt.total_seconds() / 60
    )

    df_raw["trade_duration_min"] = duration_minutes.round(2)
    df_raw["trade_duration_bars"] = (
        duration_minutes / CANDLE_MINUTES
    ).round().astype("Int64")
else:
    df_raw["trade_duration_min"] = None
    df_raw["trade_duration_bars"] = None

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
signal_trend = status["signal_trend"]
signal_direction = status["signal_direction"]
signal_momentum = status["signal_momentum"]

last_plan_status = status["last_plan_status"]
last_plan_reason = status["last_plan_reason"]
last_plan_side = status["last_plan_side"]
last_plan_entry = status["last_plan_entry"]
last_plan_tp = status["last_plan_tp"]
last_plan_sl = status["last_plan_sl"]

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

    c7, c8, c9 = st.columns(3)

    with c7:
        render_signal_text(
            signal=last_signal,
            trend=signal_trend,
            direction=signal_direction,
            momentum=signal_momentum,
        )

    with c8:
        render_plan_text(
            status=last_plan_status,
            reason=last_plan_reason,
            side=last_plan_side,
            entry=last_plan_entry,
            tp=last_plan_tp,
            sl=last_plan_sl,
        )

    with c9:
        st.metric("SYMBOL", symbol_from_status)

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

if "trade_duration_min" in table_df.columns:
    table_df["trade_duration_min"] = table_df["trade_duration_min"].map(
        lambda x: f"{x:.2f}" if pd.notnull(x) else "-"
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