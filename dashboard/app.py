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
from dashboard.analytics.mfe_mae import build_mfe_mae_report

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
def fmt_price_for_display(x, decimals=10):
    if x in (None, "", "N/A"):
        return "-"

    try:
        return f"{float(x):.{decimals}f}".rstrip("0").rstrip(".")
    except Exception:
        return str(x)
    
def safe_metric(metrics_dict, key, is_percent=False):
    value = metrics_dict.get(key, 0)

    try:
        value = float(value)
    except Exception:
        value = 0

    if is_percent:
        return f"{value:.2f}%"

    return round(value, 2)


def safe_sum(df, col):
    if col not in df.columns or df.empty:
        return 0.0
    return round(pd.to_numeric(df[col], errors="coerce").fillna(0).sum(), 2)


def safe_mean(df, col):
    if col not in df.columns or df.empty:
        return 0.0
    series = pd.to_numeric(df[col], errors="coerce").dropna()
    if series.empty:
        return 0.0
    return round(series.mean(), 2)


def safe_max(df, col):
    if col not in df.columns or df.empty:
        return 0.0
    series = pd.to_numeric(df[col], errors="coerce").dropna()
    if series.empty:
        return 0.0
    return round(series.max(), 2)

def profit_factor(x):
    wins = x[x > 0].sum()
    losses = abs(x[x < 0].sum())
    return round(wins / losses, 2) if losses > 0 else None


def build_btc_direction_matrix(df: pd.DataFrame) -> pd.DataFrame:
    required_cols = [
        "side",
        "btc_direction_1h",
        "btc_direction_15m",
        "pnl",
    ]

    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        return pd.DataFrame()

    data = df.copy()

    data["side"] = data["side"].astype(str).str.upper().str.strip()
    data["btc_direction_1h"] = data["btc_direction_1h"].astype(str).str.upper().str.strip()
    data["btc_direction_15m"] = data["btc_direction_15m"].astype(str).str.upper().str.strip()
    data["pnl"] = pd.to_numeric(data["pnl"], errors="coerce")

    data = data.dropna(subset=["side", "btc_direction_1h", "btc_direction_15m", "pnl"])

    matrix = (
        data
        .groupby(["btc_direction_1h", "btc_direction_15m", "side"])
        .agg(
            trades=("pnl", "count"),
            wins=("pnl", lambda x: int((x > 0).sum())),
            losses=("pnl", lambda x: int((x <= 0).sum())),
            winrate=("pnl", lambda x: round((x > 0).mean() * 100, 2)),
            avg_pnl=("pnl", "mean"),
            total_pnl=("pnl", "sum"),
            avg_win=("pnl", lambda x: round(x[x > 0].mean(), 3) if (x > 0).any() else 0),
            avg_loss=("pnl", lambda x: round(x[x <= 0].mean(), 3) if (x <= 0).any() else 0),
            pf=("pnl", profit_factor),
        )
        .reset_index()
    )

    for col in ["avg_pnl", "total_pnl"]:
        matrix[col] = matrix[col].round(3)

    return matrix


def build_btc_direction_pivot(matrix: pd.DataFrame) -> pd.DataFrame:
    if matrix.empty:
        return pd.DataFrame()

    pivot = matrix.pivot_table(
        index=["btc_direction_1h", "btc_direction_15m"],
        columns="side",
        values=["trades", "winrate", "avg_pnl", "total_pnl", "pf"],
        fill_value=0,
    )

    pivot.columns = [f"{metric}_{side}" for metric, side in pivot.columns]
    pivot = pivot.reset_index()

    return pivot


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


def render_signal_text(signal: str, trend: str, direction: str, momentum: str, reason: str):
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
    
    reason = reason if reason not in (None, "", "N/A") else "-"
    
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
    st.markdown(
        f"""
        <div style="line-height: 1.1;">
            <div style="font-size: 0.90rem; opacity: 1; color: white">
                REASON
            </div>
            <div style="font-size: 1rem; font-weight: 800; color: {color}; margin-top: 8px;">
                {reason}
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )


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
        return fmt_price_for_display(x)

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


def get_today_pnl_usd(df, tz_name):
    if df.empty or "exit_ts" not in df.columns or "pnl_usd" not in df.columns:
        return 0.0

    exit_dt = pd.to_datetime(df["exit_ts"], utc=True, errors="coerce")
    exit_dt = exit_dt.dt.tz_convert(tz_name)

    today = pd.Timestamp.now(tz=tz_name).date()
    mask = exit_dt.dt.date == today

    return round(df.loc[mask, "pnl_usd"].fillna(0).sum(), 2)


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
            "open_positions": raw.get("open_positions", []),

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
            
            "strategy_mode": raw.get("strategy_mode"),
            "last_router_reason": raw.get("last_router_reason"),

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
    "pnl_usd",
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
        
mfe_report = build_mfe_mae_report(df_raw)

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
# SIGNAL DELAY
# =========================

if (
    "signal_ts_dt" in df_raw.columns
    and "entry_ts_dt" in df_raw.columns
):
    df_raw["signal_delay_min"] = (
        (
            df_raw["entry_ts_dt"]
            - df_raw["signal_ts_dt"]
        ).dt.total_seconds()
        / 60
    ).round(2)
else:
    df_raw["signal_delay_min"] = None

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
open_positions = status.get("open_positions", [])

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

strategy_mode = status["strategy_mode"]
last_router_reason = status["last_router_reason"]

updated_at = status["updated_at"]

if last_signal in (None, "", "N/A"):
    last_signal = get_last_signal(df_raw)

pnl_today = get_today_pnl(df_raw, TZ)
pnl_today_usd = get_today_pnl_usd(df_raw, TZ)

# =========================
# SYSTEM STATUS
# =========================
st.markdown("## 🧠 System Status")

with st.container(border=True):

    c1, c2, c3, c4, c5 = st.columns(5)

    with c1:
        render_status_dot("ENGINE", engine_online)

    with c2:
        render_status_dot("WS", ws_online)

    c3.metric("POSITION", position_side)
    c4.metric("uPnL", round(unpnl, 2))
    c5.metric("BALANCE", f"{balance} USDT")
    #c6.metric("PNL TODAY %", pnl_today)
    #c6.metric("PNL TODAY USD", f"{pnl_today_usd} USDT")

    c8, c9, c10, c11= st.columns(4)
    
    with c8:
        st.metric(
                "STRATEGY",
                status.get("strategy_mode", "N/A")
        )

    with c9:
        render_signal_text(
            signal=last_signal,
            trend=signal_trend,
            direction=signal_direction,
            momentum=signal_momentum,
            reason=last_router_reason
        )

    with c10:
        render_plan_text(
            status=last_plan_status,
            reason=last_plan_reason,
            side=last_plan_side,
            entry=last_plan_entry,
            tp=last_plan_tp,
            sl=last_plan_sl,
        )
        
    with c11:
        st.metric("SYMBOL", symbol_from_status)

    # =========================
    # ENGINE HEALTH
    # =========================
    if status["error"]:
        st.error(f"❌ Status file error: {status['error']}")
    elif status["is_stale"]:
        st.warning("⚠️ Bot heartbeat stale or stopped")

    if updated_at:
        st.caption(f"Last heartbeat: {updated_at}")

    if open_positions:

        st.success(
            f"🟢 {len(open_positions)} open position(s) on exchange"
        )

        positions_df = pd.DataFrame(open_positions)

        numeric_cols = [
            "quantity",
            "entry_price",
            "mark_price",
            "unrealized_pnl",
        ]

        for col in numeric_cols:
            if col in positions_df.columns:
                positions_df[col] = pd.to_numeric(
                    positions_df[col],
                    errors="coerce"
                ).round(4)

        st.dataframe(
            positions_df,
            use_container_width=True
        )

    else:
        st.info("⚪ No open positions on exchange")

# =========================
# NO TRADES YET
# =========================
if df_raw.empty:
    st.markdown("---")
    st.info("📭 No trades yet")
    st.stop()
    
tab_overview, tab_mfe_mae, tab_setups, tab_bad_decisions, tab_execution = st.tabs([
    "📊 Overview",
    "📐 MFE / MAE",
    "🧠 Setups",
    "❌ Bad Decisions",
    "⏱️ Execution Analysis",
])

with tab_overview:
# =========================
# QUICK METRICS
# =========================
    st.markdown("## Overview")

    col1, col2, col3, col4, col5, col6 = st.columns(6)

    total_trades = len(df_raw)
    net_pnl_pct = safe_sum(df_raw, "pnl")
    net_pnl_usd = safe_sum(df_raw, "pnl_usd")
    winrate = round((df_raw["pnl"] > 0).mean() * 100, 2) if len(df_raw) and "pnl" in df_raw.columns else 0

    col1.metric("Trades", total_trades)
    col2.metric("Net PnL %", f"{net_pnl_pct}%")
    col3.metric("Net PnL USD", f"{net_pnl_usd} USDT")
    col4.metric("Winrate", f"{winrate}%")
    col5.metric("Avg PnL %", safe_mean(df_raw, "pnl"))
    col6.metric("Best Trade %", safe_max(df_raw, "pnl"))
    
        # =========================
    # WEEKDAY VS WEEKEND
    # =========================
    st.markdown("---")
    st.subheader("📅 Weekday vs Weekend Performance")

    if "entry_ts_dt" in df_raw.columns and "pnl" in df_raw.columns:
        day_df = df_raw.dropna(subset=["entry_ts_dt"]).copy()

        day_df["weekday_num"] = day_df["entry_ts_dt"].dt.dayofweek
        day_df["day_name"] = day_df["entry_ts_dt"].dt.day_name()
        day_df["period"] = day_df["weekday_num"].apply(
            lambda x: "Weekend" if x >= 5 else "Weekday"
        )

        period_summary = (
            day_df
            .groupby("period")
            .agg(
                trades=("pnl", "count"),
                wins=("pnl", lambda x: int((x > 0).sum())),
                losses=("pnl", lambda x: int((x <= 0).sum())),
                winrate=("pnl", lambda x: round((x > 0).mean() * 100, 2)),
                avg_pnl=("pnl", "mean"),
                net_pnl=("pnl", "sum"),
                avg_win=("pnl", lambda x: round(x[x > 0].mean(), 3) if (x > 0).any() else 0),
                avg_loss=("pnl", lambda x: round(x[x <= 0].mean(), 3) if (x <= 0).any() else 0),
            )
            .reset_index()
        )

        for col in ["avg_pnl", "net_pnl"]:
            period_summary[col] = period_summary[col].round(3)

        st.dataframe(period_summary, use_container_width=True)

        st.markdown("### 📆 Performance by Day")

        day_order = [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ]

        daily_summary = (
            day_df
            .groupby("day_name")
            .agg(
                trades=("pnl", "count"),
                wins=("pnl", lambda x: int((x > 0).sum())),
                losses=("pnl", lambda x: int((x <= 0).sum())),
                winrate=("pnl", lambda x: round((x > 0).mean() * 100, 2)),
                avg_pnl=("pnl", "mean"),
                net_pnl=("pnl", "sum"),
            )
            .reindex(day_order)
            .dropna(subset=["trades"])
            .reset_index()
        )

        daily_summary["trades"] = daily_summary["trades"].astype(int)

        for col in ["avg_pnl", "net_pnl"]:
            daily_summary[col] = daily_summary[col].round(3)

        st.dataframe(daily_summary, use_container_width=True)
        
        st.markdown("### 📊 Performance by Volume Tier")

        if "volume_tier" in df_raw.columns:

            tier_summary = (
                df_raw
                .dropna(subset=["volume_tier"])
                .groupby("volume_tier")
                .agg(
                    trades=("pnl", "count"),
                    wins=("pnl", lambda x: int((x > 0).sum())),
                    losses=("pnl", lambda x: int((x <= 0).sum())),
                    winrate=("pnl", lambda x: round((x > 0).mean() * 100, 2)),
                    avg_pnl=("pnl", "mean"),
                    net_pnl=("pnl", "sum"),
                )
                .reset_index()
            )

            tier_summary["avg_pnl"] = tier_summary["avg_pnl"].round(3)
            tier_summary["net_pnl"] = tier_summary["net_pnl"].round(3)

            st.dataframe(
                tier_summary,
                use_container_width=True
            )
            
            st.markdown("### 🚀 Performance by RVOL Tier (15m)")

        if "rvol_tier_15m" in df_raw.columns:

            rvol_summary = (
                df_raw
                .dropna(subset=["rvol_tier_15m"])
                .groupby("rvol_tier_15m")
                .agg(
                    trades=("pnl", "count"),
                    wins=("pnl", lambda x: int((x > 0).sum())),
                    losses=("pnl", lambda x: int((x <= 0).sum())),
                    winrate=("pnl", lambda x: round((x > 0).mean() * 100, 2)),
                    avg_pnl=("pnl", "mean"),
                    net_pnl=("pnl", "sum"),
                )
                .reset_index()
            )

            rvol_summary["avg_pnl"] = rvol_summary["avg_pnl"].round(3)
            rvol_summary["net_pnl"] = rvol_summary["net_pnl"].round(3)

            st.dataframe(
                rvol_summary,
                use_container_width=True
            )

    else:
        st.info("entry_ts_dt or pnl column not found.")

    # =========================
    # METRICS CALCULATION
    # =========================
    all_trades = df_raw.to_dict(orient="records")
    long_df = df_raw[df_raw["side"] == "LONG"] if "side" in df_raw.columns else pd.DataFrame()
    short_df = df_raw[df_raw["side"] == "SHORT"] if "side" in df_raw.columns else pd.DataFrame()

    long_trades = long_df.to_dict(orient="records")
    short_trades = short_df.to_dict(orient="records")

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

        l1, l2, l3, l4 = st.columns(4)
        l1.metric("Trades", safe_metric(metrics_long, "trades"))
        l2.metric("Winrate", safe_metric(metrics_long, "winrate", True))
        l3.metric("Net PnL %", safe_metric(metrics_long, "net_pnl"))
        l4.metric("Net PnL USD", f"{safe_sum(long_df, 'pnl_usd')} USDT")

        l5, l6 = st.columns(2)
        l5.metric("Avg Win", safe_metric(metrics_long, "avg_win"))
        l6.metric("Avg Loss", safe_metric(metrics_long, "avg_loss"))

        l7, l8 = st.columns(2)
        l7.metric("Expectancy", safe_metric(metrics_long, "expectancy"))
        l8.metric("Max Drawdown", safe_metric(metrics_long, "max_drawdown"))

    with col_short:
        st.markdown("## 🔴 SHORT")

        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Trades", safe_metric(metrics_short, "trades"))
        s2.metric("Winrate", safe_metric(metrics_short, "winrate", True))
        s3.metric("Net PnL %", safe_metric(metrics_short, "net_pnl"))
        s4.metric("Net PnL USD", f"{safe_sum(short_df, 'pnl_usd')} USDT")

        s5, s6 = st.columns(2)
        s5.metric("Avg Win", safe_metric(metrics_short, "avg_win"))
        s6.metric("Avg Loss", safe_metric(metrics_short, "avg_loss"))

        s7, s8 = st.columns(2)
        s7.metric("Expectancy", safe_metric(metrics_short, "expectancy"))
        s8.metric("Max Drawdown", safe_metric(metrics_short, "max_drawdown"))
        
        # =========================
    # BTC DIRECTION MATRIX
    # =========================
    st.markdown("---")
    st.subheader("₿ BTC Direction Matrix")

    btc_matrix = build_btc_direction_matrix(df_raw)

    if btc_matrix.empty:
        st.info("Missing BTC direction columns or not enough data.")
    else:
        btc_pivot = build_btc_direction_pivot(btc_matrix)

        st.markdown("### BTC 1H + BTC 15M: LONG vs SHORT")

        st.dataframe(
            btc_pivot.sort_values(["btc_direction_1h", "btc_direction_15m"]),
            use_container_width=True,
        )

        st.markdown("### Detailed Matrix")

        st.dataframe(
            btc_matrix.sort_values(
                ["btc_direction_1h", "btc_direction_15m", "side"]
            ),
            use_container_width=True,
        )

        st.caption(
            "Lectura rápida: BTC 1H UP + BTC 15M UP debería favorecer LONG. "
            "BTC 1H DOWN + BTC 15M DOWN debería favorecer SHORT. "
            "Los regímenes mixtos suelen ser zonas de cuidado."
        )

    # =========================
    # FORMAT DATES FOR DISPLAY ONLY
    # =========================
    df_display = df_raw.copy()

    for col in ["signal_ts", "entry_ts", "exit_ts"]:
        dt_col = f"{col}_dt"

        if dt_col in df_display.columns:
            df_display[col] = df_display[dt_col].dt.strftime("%d-%m %H:%M")
        
with tab_mfe_mae:
    st.markdown("---")
    st.subheader("📐 MFE / MAE Analytics")

    if not mfe_report:
        st.info("No MFE/MAE data available yet.")
    else:
        c1, c2, c3 = st.columns(3)

        capture = mfe_report["capture"]

        c1.metric("Trades analyzed", mfe_report["total_trades"])
        c2.metric("Avg Capture Ratio", f"{capture['avg_capture_ratio']}%")
        c3.metric("Median Capture Ratio", f"{capture['median_capture_ratio']}%")

        st.markdown("### MFE Distribution")

        mfe_dist_df = pd.DataFrame.from_dict(
            mfe_report["mfe_distribution"],
            orient="index"
        )

        st.dataframe(mfe_dist_df, use_container_width=True)

        st.markdown("### TP vs SL Excursion")

        result_df = pd.DataFrame.from_dict(
            mfe_report["result_comparison"],
            orient="index"
        )

        st.dataframe(result_df, use_container_width=True)

        st.markdown("### SL Trades: MFE Before Loss")

        sl_dist_df = pd.DataFrame.from_dict(
            mfe_report["sl_mfe_distribution"],
            orient="index"
        )

        st.dataframe(sl_dist_df, use_container_width=True)

        col_a, col_b = st.columns(2)

        with col_a:
            st.markdown("### Top Symbols by Avg MFE")
            st.dataframe(
                pd.DataFrame(mfe_report["top_symbols_by_mfe"]),
                use_container_width=True
            )

        with col_b:
            st.markdown("### Worst Symbols by Avg MFE")
            st.dataframe(
                pd.DataFrame(mfe_report["worst_symbols_by_mfe"]),
                use_container_width=True
            )
with tab_setups:

    st.markdown("---")
    st.subheader("🧠 Setups: Direction + Momentum")

    required_cols = [
        "side",
        "signal_direction",
        "signal_momentum",
        "pnl",
        "max_favorable_pct",
        "max_adverse_pct",
    ]

    missing_cols = [c for c in required_cols if c not in df_raw.columns]

    if missing_cols:
        st.info(f"Missing columns: {missing_cols}")

    else:
        def profit_factor(x):
            wins = x[x > 0].sum()
            losses = abs(x[x < 0].sum())
            return round(wins / losses, 2) if losses > 0 else None

        min_trades = st.slider(
            "Minimum trades per setup",
            min_value=1,
            max_value=30,
            value=5,
            step=1,
        )

        setup_df = (
            df_raw
            .dropna(subset=["signal_direction", "signal_momentum", "side"])
            .groupby(["side", "signal_direction", "signal_momentum"])
            .agg(
                trades=("pnl", "count"),
                wins=("pnl", lambda x: int((x > 0).sum())),
                losses=("pnl", lambda x: int((x <= 0).sum())),
                winrate=("pnl", lambda x: round((x > 0).mean() * 100, 2)),
                avg_pnl=("pnl", "mean"),
                total_pnl=("pnl", "sum"),
                avg_mfe=("max_favorable_pct", "mean"),
                avg_mae=("max_adverse_pct", "mean"),
                pf=("pnl", profit_factor),
            )
            .reset_index()
        )

        setup_df = setup_df[setup_df["trades"] >= min_trades]

        for col in ["avg_pnl", "total_pnl", "avg_mfe", "avg_mae"]:
            setup_df[col] = setup_df[col].round(3)

        setup_df = setup_df.sort_values(
            by=["pf", "total_pnl", "winrate"],
            ascending=False,
            na_position="last",
        )

        st.dataframe(setup_df, use_container_width=True)
        
with tab_bad_decisions:

    st.markdown("---")
    st.subheader("❌ Bad Decisions Explorer")

    required_cols = [
        "symbol",
        "side",
        "signal_direction",
        "signal_momentum",
        "pnl",
        "exit_reason",
        "max_favorable_pct",
        "max_adverse_pct",
    ]

    missing_cols = [c for c in required_cols if c not in df_raw.columns]

    if missing_cols:
        st.info(f"Missing columns: {missing_cols}")

    else:
        bad_df = df_raw.copy()

        bad_df["is_sl"] = bad_df["exit_reason"].astype(str).str.upper().eq("SL")
        bad_df["is_win"] = bad_df["pnl"] > 0

        total_trades = len(bad_df)
        total_sl = int(bad_df["is_sl"].sum())
        sl_rate = round((total_sl / total_trades) * 100, 2) if total_trades else 0
        winrate = round(bad_df["is_win"].mean() * 100, 2) if total_trades else 0

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Trades", total_trades)
        c2.metric("Total SL", total_sl)
        c3.metric("SL Rate", f"{sl_rate}%")
        c4.metric("Winrate", f"{winrate}%")

        st.markdown("### 🔥 Worst Setups")

        def profit_factor(x):
            wins = x[x > 0].sum()
            losses = abs(x[x < 0].sum())
            return round(wins / losses, 2) if losses > 0 else None

        min_trades = st.slider(
            "Minimum trades per setup",
            min_value=1,
            max_value=30,
            value=5,
            step=1,
            key="bad_decisions_min_trades",
        )

        worst_setups = (
            bad_df
            .dropna(subset=["side", "signal_direction", "signal_momentum"])
            .groupby(["side", "signal_direction", "signal_momentum"])
            .agg(
                trades=("pnl", "count"),
                sl_count=("is_sl", "sum"),
                winrate=("is_win", lambda x: round(x.mean() * 100, 2)),
                sl_rate=("is_sl", lambda x: round(x.mean() * 100, 2)),
                avg_pnl=("pnl", "mean"),
                total_pnl=("pnl", "sum"),
                avg_mfe=("max_favorable_pct", "mean"),
                avg_mae=("max_adverse_pct", "mean"),
                pf=("pnl", profit_factor),
            )
            .reset_index()
        )

        worst_setups = worst_setups[worst_setups["trades"] >= min_trades]

        for col in ["avg_pnl", "total_pnl", "avg_mfe", "avg_mae"]:
            worst_setups[col] = worst_setups[col].round(3)

        worst_setups = worst_setups.sort_values(
            by=["sl_rate", "total_pnl"],
            ascending=[False, True],
            na_position="last",
        )

        st.dataframe(worst_setups, use_container_width=True)

        st.markdown("### 🌎 SL Rate by BTC Context")

        if "btc_context_state" in bad_df.columns:
            btc_bad = (
                bad_df
                .dropna(subset=["btc_context_state"])
                .groupby("btc_context_state")
                .agg(
                    trades=("pnl", "count"),
                    sl_count=("is_sl", "sum"),
                    winrate=("is_win", lambda x: round(x.mean() * 100, 2)),
                    sl_rate=("is_sl", lambda x: round(x.mean() * 100, 2)),
                    avg_pnl=("pnl", "mean"),
                )
                .reset_index()
                .sort_values("sl_rate", ascending=False)
            )

            btc_bad["avg_pnl"] = btc_bad["avg_pnl"].round(3)
            st.dataframe(btc_bad, use_container_width=True)
        else:
            st.info("btc_context_state column not found.")

        st.markdown("### 🪙 SL Rate by Relative Volume")

        if "rvol_tier_15m" in bad_df.columns:
            rvol_bad = (
                bad_df
                .dropna(subset=["rvol_tier_15m"])
                .groupby("rvol_tier_15m")
                .agg(
                    trades=("pnl", "count"),
                    sl_count=("is_sl", "sum"),
                    winrate=("is_win", lambda x: round(x.mean() * 100, 2)),
                    sl_rate=("is_sl", lambda x: round(x.mean() * 100, 2)),
                    avg_pnl=("pnl", "mean"),
                )
                .reset_index()
                .sort_values("sl_rate", ascending=False)
            )

            rvol_bad["avg_pnl"] = rvol_bad["avg_pnl"].round(3)
            st.dataframe(rvol_bad, use_container_width=True)
        else:
            st.info("rvol_tier_15m column not found.")

        st.markdown("### 🚨 Worst Symbols")

        symbol_bad = (
            bad_df
            .dropna(subset=["symbol"])
            .groupby("symbol")
            .agg(
                trades=("pnl", "count"),
                sl_count=("is_sl", "sum"),
                winrate=("is_win", lambda x: round(x.mean() * 100, 2)),
                sl_rate=("is_sl", lambda x: round(x.mean() * 100, 2)),
                avg_pnl=("pnl", "mean"),
                total_pnl=("pnl", "sum"),
            )
            .reset_index()
        )

        symbol_bad["avg_pnl"] = symbol_bad["avg_pnl"].round(3)
        symbol_bad["total_pnl"] = symbol_bad["total_pnl"].round(3)

        symbol_bad = symbol_bad.sort_values(
            by=["sl_rate", "total_pnl"],
            ascending=[False, True],
        )

        st.dataframe(symbol_bad, use_container_width=True)

        st.markdown("### ⏰ SL Rate by Entry Hour")

        if "entry_ts_dt" in bad_df.columns:
            hour_df = bad_df.dropna(subset=["entry_ts_dt"]).copy()
            hour_df["entry_hour"] = hour_df["entry_ts_dt"].dt.hour

            hour_bad = (
                hour_df
                .groupby("entry_hour")
                .agg(
                    trades=("pnl", "count"),
                    sl_count=("is_sl", "sum"),
                    winrate=("is_win", lambda x: round(x.mean() * 100, 2)),
                    sl_rate=("is_sl", lambda x: round(x.mean() * 100, 2)),
                    avg_pnl=("pnl", "mean"),
                )
                .reset_index()
                .sort_values("entry_hour")
            )

            hour_bad["avg_pnl"] = hour_bad["avg_pnl"].round(3)
            st.dataframe(hour_bad, use_container_width=True)
        else:
            st.info("entry_ts_dt column not found.")

        st.markdown("### 📈 EMA Extension Risk")

        ema_cols = [
            "dist_ema20_15m_pct",
            "dist_ema20_1h_pct",
            "dist_ema20_4h_pct",
        ]

        available_ema_cols = [c for c in ema_cols if c in bad_df.columns]

        if available_ema_cols:
            ema_col = st.selectbox(
                "EMA distance column",
                available_ema_cols,
                key="bad_decisions_ema_col",
            )

            ema_df = bad_df.copy()
            ema_df[ema_col] = pd.to_numeric(ema_df[ema_col], errors="coerce")

            ema_df["ema_distance_bucket"] = pd.cut(
                ema_df[ema_col].abs(),
                bins=[0, 0.3, 0.8, 1.5, float("inf")],
                labels=["0-0.3%", "0.3-0.8%", "0.8-1.5%", ">1.5%"],
                include_lowest=True,
            )

            ema_bad = (
                ema_df
                .dropna(subset=["ema_distance_bucket"])
                .groupby("ema_distance_bucket", observed=True)
                .agg(
                    trades=("pnl", "count"),
                    sl_count=("is_sl", "sum"),
                    winrate=("is_win", lambda x: round(x.mean() * 100, 2)),
                    sl_rate=("is_sl", lambda x: round(x.mean() * 100, 2)),
                    avg_pnl=("pnl", "mean"),
                )
                .reset_index()
            )

            ema_bad["avg_pnl"] = ema_bad["avg_pnl"].round(3)
            st.dataframe(ema_bad, use_container_width=True)
        else:
            st.info("No EMA distance columns found.")

        st.markdown("### 🧨 Last SL Trades")

        sl_only = bad_df[bad_df["is_sl"]].copy()

        cols_to_show = [
            "symbol",
            "side",
            "entry_ts",
            "signal_direction",
            "signal_momentum",
            "btc_context_state",
            "rvol_tier_15m",
            "pnl",
            "max_favorable_pct",
            "max_adverse_pct",
            "exit_reason",
        ]

        cols_to_show = [c for c in cols_to_show if c in sl_only.columns]

        if "entry_ts_dt" in sl_only.columns:
            sl_only = sl_only.sort_values("entry_ts_dt", ascending=False)

        st.dataframe(
            sl_only[cols_to_show].head(20),
            use_container_width=True,
        )
    
        st.markdown("### 🎯 Swing Context Risk")
        
        swing_analysis = []

        for tf in ["15m", "1h", "4h"]:

            high_col = f"near_swing_high_{tf}"
            low_col = f"near_swing_low_{tf}"

            if high_col in df_raw.columns:

                subset = df_raw[
                    (df_raw["side"] == "LONG")
                    & (df_raw[high_col] == True)
                ]

                swing_analysis.append({
                    "setup": f"LONG near swing high {tf}",
                    "trades": len(subset),
                    "sl": (subset["exit_reason"] == "SL").sum(),
                    "winrate": round((subset["pnl"] > 0).mean() * 100, 2)
                    if len(subset) else 0
                })

            if low_col in df_raw.columns:

                subset = df_raw[
                    (df_raw["side"] == "SHORT")
                    & (df_raw[low_col] == True)
                ]

                swing_analysis.append({
                    "setup": f"SHORT near swing low {tf}",
                    "trades": len(subset),
                    "sl": (subset["exit_reason"] == "SL").sum(),
                    "winrate": round((subset["pnl"] > 0).mean() * 100, 2)
                    if len(subset) else 0
                })

            st.dataframe(
                pd.DataFrame(swing_analysis),
                use_container_width=True
            )
            
with tab_execution:

    try:

        st.markdown("---")
        st.subheader("⏱️ Signal → Entry Execution Analysis")

        if (
            "signal_delay_min" not in df_raw.columns
            or df_raw["signal_delay_min"].isna().all()
        ):
            st.info("No signal delay data available.")

        else:

            c1, c2, c3, c4 = st.columns(4)

            c1.metric(
                "Avg Delay",
                f"{df_raw['signal_delay_min'].mean():.2f} min"
            )

            c2.metric(
                "Median Delay",
                f"{df_raw['signal_delay_min'].median():.2f} min"
            )

            c3.metric(
                "Max Delay",
                f"{df_raw['signal_delay_min'].max():.2f} min"
            )

            c4.metric(
                "Trades",
                len(df_raw)
            )

            # =========================
            # DELAY BY DAY
            # =========================

            st.markdown("### Delay by Day")

            delay_by_day = (
                df_raw
                .dropna(subset=["entry_ts_dt", "signal_delay_min"])
                .groupby(df_raw["entry_ts_dt"].dt.date)
                .agg(
                    trades=("pnl", "count"),
                    avg_delay=("signal_delay_min", "mean"),
                    median_delay=("signal_delay_min", "median"),
                    max_delay=("signal_delay_min", "max"),
                    winrate=("pnl", lambda x: round((x > 0).mean() * 100, 2)),
                )
                .reset_index()
            )

            delay_by_day["avg_delay"] = delay_by_day["avg_delay"].round(2)
            delay_by_day["median_delay"] = delay_by_day["median_delay"].round(2)
            delay_by_day["max_delay"] = delay_by_day["max_delay"].round(2)

            st.dataframe(
                delay_by_day,
                use_container_width=True
            )

            # =========================
            # DELAY BUCKETS
            # =========================

            st.markdown("### Winrate by Delay Bucket")

            bucket_df = df_raw.copy()

            bucket_df["delay_bucket"] = pd.cut(
                bucket_df["signal_delay_min"],
                bins=[0, 1, 2, 5, 10, 1000],
                labels=[
                    "<1m",
                    "1-2m",
                    "2-5m",
                    "5-10m",
                    ">10m"
                ]
            )

            delay_stats = (
                bucket_df
                .dropna(subset=["delay_bucket"])
                .groupby("delay_bucket", observed=True)
                .agg(
                    trades=("pnl", "count"),
                    winrate=("pnl", lambda x: round((x > 0).mean() * 100, 2)),
                    avg_pnl=("pnl", "mean"),
                    total_pnl=("pnl", "sum"),
                )
                .reset_index()
            )

            delay_stats["avg_pnl"] = delay_stats["avg_pnl"].round(3)
            delay_stats["total_pnl"] = delay_stats["total_pnl"].round(3)

            st.dataframe(
                delay_stats,
                use_container_width=True
            )

            # =========================
            # DELAY BY SYMBOL
            # =========================

            st.markdown("### Delay by Symbol")

            symbol_delay = (
                df_raw
                .dropna(subset=["signal_delay_min"])
                .groupby("symbol")
                .agg(
                    trades=("pnl", "count"),
                    avg_delay=("signal_delay_min", "mean"),
                    max_delay=("signal_delay_min", "max"),
                    winrate=("pnl", lambda x: round((x > 0).mean() * 100, 2)),
                )
                .reset_index()
                .sort_values("avg_delay", ascending=False)
            )

            symbol_delay["avg_delay"] = symbol_delay["avg_delay"].round(2)
            symbol_delay["max_delay"] = symbol_delay["max_delay"].round(2)

            st.dataframe(
                symbol_delay,
                use_container_width=True
            )
            
    except Exception as e:

        st.error(f"Execution Analysis Error: {e}")
        st.exception(e)
        
# =========================
# TRADES TABLE
# =========================
st.markdown("---")
st.subheader("📋 Trades")

table_df = df_display.copy()

# =========================
# DATE FILTER
# =========================

if "entry_ts_dt" in df_raw.columns:

    filter_col1, filter_col2 = st.columns(2)

    with filter_col1:
        start_date = st.date_input(
            "Start Date",
            value=df_raw["entry_ts_dt"].min().date(),
            key="trades_start_date"
        )

    with filter_col2:
        end_date = st.date_input(
            "End Date",
            value=df_raw["entry_ts_dt"].max().date(),
            key="trades_end_date"
        )

if "entry_ts_dt" in table_df.columns:
    table_df = table_df.sort_values("entry_ts_dt")
    
if "entry_ts_dt" in table_df.columns:

    table_df = table_df[
        (
            table_df["entry_ts_dt"].dt.date >= start_date
        )
        &
        (
            table_df["entry_ts_dt"].dt.date <= end_date
        )
    ]

if "entry_distance_pct" in table_df.columns:
    table_df["entry_distance_pct"] = table_df["entry_distance_pct"].map(
        lambda x: f"{x:.4f}%" if pd.notnull(x) else "0.0000%"
    )

if "pnl" in table_df.columns:
    table_df["pnl"] = table_df["pnl"].map(
        lambda x: f"{x:.4f}%" if pd.notnull(x) else "-"
    )

if "trade_duration_min" in table_df.columns:
    table_df["trade_duration_min"] = table_df["trade_duration_min"].map(
        lambda x: f"{x:.2f}" if pd.notnull(x) else "-"
    )

drop_cols = [c for c in ["signal_ts_dt", "entry_ts_dt", "exit_ts_dt"] if c in table_df.columns]
table_df = table_df.drop(columns=drop_cols)

# =========================
# FORMAT BOOLEAN COLUMNS
# =========================

bool_cols = [
    "reclaimed_ema20_1m",
    "reclaimed_ema34_1m",
    "reclaimed_ema50_1m",
    "lost_ema20_1m",
    "lost_ema34_1m",
    "lost_ema50_1m",
    "direction_5m_changed",
]

for col in bool_cols:
    if col in table_df.columns:
        table_df[col] = table_df[col].map({
            True: "TRUE",
            False: "FALSE"
        })
        
price_cols = [
    "signal_price",
    "entry",
    "real_entry",
    "exit",
    "real_exit",
    "tp",
    "sl",
    "tp1",
    "tp2",
    "tp3",
    "entry_price",
    "mark_price",
]

for col in price_cols:
    if col in table_df.columns:
        table_df[col] = table_df[col].map(fmt_price_for_display)
        
st.caption(
    f"Showing {len(table_df)} trades "
    f"from {start_date} to {end_date}"
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

df_equity = df_raw.copy()

if "entry_ts_dt" in df_equity.columns and "pnl" in df_equity.columns:
    df_equity = df_equity.sort_values("entry_ts_dt")
    df_equity["equity"] = df_equity["pnl"].fillna(0).cumsum()

    st.line_chart(
        df_equity.set_index("entry_ts_dt")["equity"],
        use_container_width=True
    )

# =========================
# EQUITY CURVE USD
# =========================
if "entry_ts_dt" in df_raw.columns and "pnl_usd" in df_raw.columns:
    st.markdown("---")
    st.subheader("💵 Equity Curve USD")

    df_equity_usd = df_raw.copy().sort_values("entry_ts_dt")
    df_equity_usd["equity_usd"] = df_equity_usd["pnl_usd"].fillna(0).cumsum()

    st.line_chart(
        df_equity_usd.set_index("entry_ts_dt")["equity_usd"],
        use_container_width=True
    )