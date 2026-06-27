import os
import sys
import json
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from streamlit_autorefresh import st_autorefresh

import requests
import plotly.graph_objects as go


# =========================
# CONFIG
# =========================
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR))

load_dotenv(BASE_DIR / ".env")

from engine.backtest.metrics import calculate_metrics  # noqa
from dashboard.analytics.mfe_mae import build_mfe_mae_report

TRADES_FILE = BASE_DIR / "trades.csv"
PAPER_SIGNALS_FILE = BASE_DIR / "paper_signals.csv"
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
    
def load_watch_history(symbol, base_dir="compression_watch_journal", limit=50):
    path = Path(base_dir) / f"{symbol}.jsonl"

    if not path.exists():
        return pd.DataFrame()

    rows = []

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    if "logged_at" in df.columns:
        df = df.sort_values("logged_at", ascending=False)

    return df.head(limit)
    
def safe_metric(metrics_dict, key, is_percent=False):
    value = metrics_dict.get(key, 0)

    try:
        value = float(value)
    except Exception:
        value = 0

    if is_percent:
        return f"{value:.2f}%"

    return round(value, 2)

def load_compression_snapshots(base_dir="compression_snapshots"):
    base_path = Path(base_dir)

    rows = []

    if not base_path.exists():
        return pd.DataFrame()

    for path in base_path.glob("*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            rows.append(data)

        except Exception:
            continue

    return pd.DataFrame(rows)

def load_compression_pipeline(path="compression_pipeline.json"):
    path = Path(path)

    if not path.exists():
        return pd.DataFrame()

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        return pd.DataFrame(data)

    except Exception:
        return pd.DataFrame()

def filter_by_window(df, days=None, yesterday=False):
    if df.empty or "entry_ts_dt" not in df.columns:
        return pd.DataFrame()

    now = pd.Timestamp.now(tz=TZ)

    if yesterday:
        target = (now - pd.Timedelta(days=1)).date()

        return df[
            df["entry_ts_dt"].dt.date == target
        ]

    if days == 0:
        return df[
            df["entry_ts_dt"].dt.date == now.date()
        ]

    if days is None:
        return df

    start = (now - pd.Timedelta(days=days)).date()

    return df[
        df["entry_ts_dt"].dt.date >= start
    ]

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

def build_today_4h_strategy_performance(df):
    if df.empty or "entry_ts_dt" not in df.columns or "side" not in df.columns:
        return pd.DataFrame()

    today = pd.Timestamp.now(tz=TZ).date()

    data = df[
        df["entry_ts_dt"].dt.date == today
    ].copy()

    if data.empty:
        return pd.DataFrame()

    data["hour"] = data["entry_ts_dt"].dt.hour

    data["block_4h"] = pd.cut(
        data["hour"],
        bins=[0, 4, 8, 12, 16, 20, 24],
        labels=["00-04", "04-08", "08-12", "12-16", "16-20", "20-24"],
        right=False,
        include_lowest=True,
    )
    
    current_hour = pd.Timestamp.now(tz=TZ).hour
    current_block_start = (current_hour // 4) * 4
    current_block = f"{current_block_start:02d}-{current_block_start + 4:02d}"

    rows = []

    for block, block_df in data.groupby("block_4h", observed=False):
        if block_df.empty:
            continue

        long_df = block_df[block_df["side"] == "LONG"]
        short_df = block_df[block_df["side"] == "SHORT"]

        metrics_long = calculate_metrics(long_df.to_dict("records"))
        metrics_short = calculate_metrics(short_df.to_dict("records"))

        long_exp = safe_metric(metrics_long, "expectancy")
        short_exp = safe_metric(metrics_short, "expectancy")
        edge_difference = round(long_exp - short_exp, 2)

        if long_exp > 0 and short_exp < 0:
            side_bias = "🟢 LONG EDGE"
        elif long_exp < 0 and short_exp > 0:
            side_bias = "🔴 SHORT EDGE"
        elif long_exp <= 0 and short_exp <= 0:
            side_bias = "⚪ NO EDGE"
        else:
            side_bias = "🟡 BOTH SIDES"

        if long_exp <= 0 and short_exp <= 0:
            recommendation = "⚪ NO TRADE"
        elif edge_difference > 0.10:
            recommendation = "🟢 LONG ONLY"
        elif edge_difference < -0.10:
            recommendation = "🔴 SHORT ONLY"
        else:
            recommendation = "🟡 BOTH"

        rows.append({
            "block_4h": str(block),
            "status": "🟡 CURRENT / PARTIAL" if str(block) == current_block else "✅ CLOSED",
            "trades": len(block_df),

            "long_trades": safe_metric(metrics_long, "trades"),
            "long_winrate": safe_metric(metrics_long, "winrate", True),
            "long_net_pnl": safe_metric(metrics_long, "net_pnl"),
            "long_expectancy": long_exp,

            "short_trades": safe_metric(metrics_short, "trades"),
            "short_winrate": safe_metric(metrics_short, "winrate", True),
            "short_net_pnl": safe_metric(metrics_short, "net_pnl"),
            "short_expectancy": short_exp,

            "edge_difference": edge_difference,
            "side_bias": side_bias,
            "recommendation": recommendation,
        })

    return pd.DataFrame(rows)

def build_historical_4h_blocks(df):
    if df.empty or "entry_ts_dt" not in df.columns:
        return pd.DataFrame()

    data = df.copy()

    data["hour"] = data["entry_ts_dt"].dt.hour

    data["block_4h"] = pd.cut(
        data["hour"],
        bins=[0, 4, 8, 12, 16, 20, 24],
        labels=["00-04", "04-08", "08-12", "12-16", "16-20", "20-24"],
        right=False,
        include_lowest=True,
    )

    summary = (
        data
        .groupby("block_4h", observed=False)
        .agg(
            trades=("pnl", "count"),
            wins=("pnl", lambda x: int((x > 0).sum())),
            losses=("pnl", lambda x: int((x <= 0).sum())),
            winrate=("pnl", lambda x: round((x > 0).mean() * 100, 2)),
            avg_pnl=("pnl", "mean"),
            net_pnl=("pnl", "sum"),
            avg_win=("pnl", lambda x: round(x[x > 0].mean(), 3) if (x > 0).any() else 0),
            avg_loss=("pnl", lambda x: round(x[x <= 0].mean(), 3) if (x <= 0).any() else 0),
            pf=("pnl", profit_factor),
        )
        .reset_index()
    )

    for col in ["avg_pnl", "net_pnl"]:
        summary[col] = summary[col].round(3)

    return summary


def get_today_pnl(df, tz_name):
    if df.empty or "exit_ts" not in df.columns or "pnl" not in df.columns:
        return 0.0

    exit_dt = pd.to_datetime(df["exit_ts"], utc=True, errors="coerce")
    exit_dt = exit_dt.dt.tz_convert(tz_name)

    today = pd.Timestamp.now(tz=tz_name).date()
    mask = exit_dt.dt.date == today

    return round(df.loc[mask, "pnl"].fillna(0).sum(), 2)

def build_btc_regime_by_signals(trades_df, paper_df, tz_name):
    frames = []

    if not trades_df.empty:
        real = trades_df.copy()
        real["source"] = "real_trade"
        real["event_ts"] = real.get("entry_ts", real.get("signal_ts"))
        frames.append(real)

    if not paper_df.empty:
        paper = paper_df.copy()
        paper["source"] = "paper_signal"
        paper["event_ts"] = paper.get("ts")
        frames.append(paper)

    if not frames:
        return pd.DataFrame()

    data = pd.concat(frames, ignore_index=True)

    required = ["event_ts", "btc_direction_15m", "btc_direction_1h"]
    missing = [c for c in required if c not in data.columns]

    if missing:
        return pd.DataFrame()

    data["event_dt"] = pd.to_datetime(data["event_ts"], utc=True, errors="coerce")

    try:
        data["event_dt"] = data["event_dt"].dt.tz_convert(tz_name)
    except Exception:
        pass

    data = data.dropna(subset=["event_dt"])

    for col in ["btc_direction_15m", "btc_direction_1h"]:
        data[col] = data[col].astype(str).str.lower().str.strip()

    now = pd.Timestamp.now(tz=tz_name)

    windows = {
        "Today": (
            now.date(),
            now.date(),
        ),

        "Yesterday": (
            (now - pd.Timedelta(days=1)).date(),
            (now - pd.Timedelta(days=1)).date(),
        ),

        "Last 3D": (
            (now - pd.Timedelta(days=3)).date(),
            now.date(),
        ),

        "Last 7D": (
            (now - pd.Timedelta(days=7)).date(),
            now.date(),
        ),

        "Last 30D": (
            (now - pd.Timedelta(days=30)).date(),
            now.date(),
        ),
    }

    rows = []

    for label, (start_date, end_date) in windows.items():
        subset = data[
            (data["event_dt"].dt.date >= start_date)
            & (data["event_dt"].dt.date <= end_date)
        ]

        total = len(subset)

        if total == 0:
            rows.append({
                "window": label,
                "signals": 0,
                "btc_15m_up_pct": 0,
                "btc_15m_down_pct": 0,
                "btc_1h_up_pct": 0,
                "btc_1h_down_pct": 0,
                "long_signals": 0,
                "short_signals": 0,
                "paper_signals": 0,
            })
            continue

        side = subset["side"].astype(str).str.upper().str.strip() if "side" in subset.columns else pd.Series([], dtype=str)

        rows.append({
            "window": label,
            "signals": total,
            "btc_15m_up_pct": round((subset["btc_direction_15m"].eq("up").mean()) * 100, 2),
            "btc_15m_down_pct": round((subset["btc_direction_15m"].eq("down").mean()) * 100, 2),
            "btc_1h_up_pct": round((subset["btc_direction_1h"].eq("up").mean()) * 100, 2),
            "btc_1h_down_pct": round((subset["btc_direction_1h"].eq("down").mean()) * 100, 2),
            "long_signals": int((side == "LONG").sum()),
            "short_signals": int((side == "SHORT").sum()),
            "paper_signals": int((subset["source"] == "paper_signal").sum()),
        })

    return pd.DataFrame(rows)


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

if PAPER_SIGNALS_FILE.exists():
    paper_df = pd.read_csv(PAPER_SIGNALS_FILE)
else:
    paper_df = pd.DataFrame()

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
# GLOBAL VIEW (NO FILTER YET)
# =========================
df_view = df_raw.copy()

# =========================
# GLOBAL DATE FILTER
# =========================
if "entry_ts_dt" in df_view.columns and not df_view.empty:

    st.sidebar.markdown("## 📅 Trade Filter")

    start_date = st.sidebar.date_input(
        "Start Date",
        value=df_view["entry_ts_dt"].min().date(),
        key="global_start_date"
    )

    end_date = st.sidebar.date_input(
        "End Date",
        value=df_view["entry_ts_dt"].max().date(),
        key="global_end_date"
    )

    df_view = df_view[
        (df_view["entry_ts_dt"].dt.date >= start_date)
        &
        (df_view["entry_ts_dt"].dt.date <= end_date)
    ].copy()
    
st.sidebar.caption(f"Filtered trades: {len(df_view)}")

# =========================
# MFE / MAE REPORT
# =========================
mfe_report = build_mfe_mae_report(df_view)

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
    
tab_overview, tab_mfe_mae, tab_setups, tab_swings, tab_bad_decisions, tab_execution, tab_compressions, tab_compression_pipeline = st.tabs([
    "📊 Overview",
    "📐 MFE / MAE",
    "🧠 Setups",
    "🎯 Swings",
    "❌ Bad Decisions x",
    "⏱️ Execution Analysis",
    "Compressions",
    "Compression Pipeline"
])

with tab_overview:
# =========================
# QUICK METRICS
# =========================
    st.markdown("## Overview")
    
    # =========================
    # BTC REGIME BY SIGNALS
    # =========================
    st.markdown("---")
    st.subheader("₿ BTC Regime by Signals")

    btc_regime_signals = build_btc_regime_by_signals(
        trades_df=df_raw,
        paper_df=paper_df,
        tz_name=TZ,
    )

    if btc_regime_signals.empty:
        st.info("No BTC regime signal data available yet.")
    else:
        today = btc_regime_signals[
            btc_regime_signals["window"] == "Today"
        ]

        if not today.empty:
            row = today.iloc[0]

            c1, c2, c3, c4, c5, c6 = st.columns(6)

            c1.metric("Signals Today", int(row["signals"]))
            c2.metric("BTC 15m UP", f"{row['btc_15m_up_pct']}%")
            c3.metric("BTC 15m DOWN", f"{row['btc_15m_down_pct']}%")
            c4.metric("BTC 1h UP", f"{row['btc_1h_up_pct']}%")
            c5.metric("BTC 1h DOWN", f"{row['btc_1h_down_pct']}%")
            c6.metric("Paper Signals", int(row["paper_signals"]))

        st.dataframe(
            btc_regime_signals,
            use_container_width=True,
        )

        st.caption(
            "Incluye trades reales + señales paper bloqueadas. "
            "Sirve para detectar si el sistema está recibiendo más señales en contexto BTC bullish o bearish."
        )

    col1, col2, col3, col4, col5, col6 = st.columns(6)

    total_trades = len(df_view)
    net_pnl_pct = safe_sum(df_view, "pnl")
    net_pnl_usd = safe_sum(df_view, "pnl_usd")
    winrate = round((df_view["pnl"] > 0).mean() * 100, 2) if len(df_raw) and "pnl" in df_view.columns else 0

    col1.metric("Trades", total_trades)
    col2.metric("Net PnL %", f"{net_pnl_pct}%")
    col3.metric("Net PnL USD", f"{net_pnl_usd} USDT")
    col4.metric("Winrate", f"{winrate}%")
    col5.metric("Avg PnL %", safe_mean(df_view, "pnl"))
    col6.metric("Best Trade %", safe_max(df_view, "pnl"))
        
        #st.markdown("### 📊 Performance by Volume Tier")

        #if "volume_tier" in df_raw.columns:

          #  tier_summary = (
          #      df_raw
          #      .dropna(subset=["volume_tier"])
           #     .groupby("volume_tier")
            #    .agg(
             #       trades=("pnl", "count"),
              #      wins=("pnl", lambda x: int((x > 0).sum())),
                #    losses=("pnl", lambda x: int((x <= 0).sum())),
               #     winrate=("pnl", lambda x: round((x > 0).mean() * 100, 2)),
                 #   avg_pnl=("pnl", "mean"),
                  #  net_pnl=("pnl", "sum"),
                #)
                #.reset_index()
            #)

            #tier_summary["avg_pnl"] = tier_summary["avg_pnl"].round(3)
            #tier_summary["net_pnl"] = tier_summary["net_pnl"].round(3)

#            st.dataframe(
 #               tier_summary,
  #              use_container_width=True
   #         )
            
    #        st.markdown("### 🚀 Performance by RVOL Tier (15m)")

     #   if "rvol_tier_15m" in df_raw.columns:
#
   #         rvol_summary = (
    #            df_raw
     #           .dropna(subset=["rvol_tier_15m"])
      #          .groupby("rvol_tier_15m")
       #         .agg(
        #            trades=("pnl", "count"),
         ##           wins=("pnl", lambda x: int((x > 0).sum())),
           #         losses=("pnl", lambda x: int((x <= 0).sum())),
            #        winrate=("pnl", lambda x: round((x > 0).mean() * 100, 2)),
             ##       avg_pnl=("pnl", "mean"),
              #      net_pnl=("pnl", "sum"),
              #  )
           #     .reset_index()
            #)

         #   rvol_summary["avg_pnl"] = rvol_summary["avg_pnl"].round(3)
          #  rvol_summary["net_pnl"] = rvol_summary["net_pnl"].round(3)

           # st.dataframe(
            #    rvol_summary,
             #   use_container_width=True
          #  )

    #else:
     #   st.info("entry_ts_dt or pnl column not found.")

    # =========================
    # METRICS CALCULATION
    # =========================
    all_trades = df_view.to_dict(orient="records")
    long_df = df_view[df_view["side"] == "LONG"] if "side" in df_view.columns else pd.DataFrame()
    short_df = df_view[df_view["side"] == "SHORT"] if "side" in df_view.columns else pd.DataFrame()

    long_trades = long_df.to_dict(orient="records")
    short_trades = short_df.to_dict(orient="records")

    metrics_all = calculate_metrics(all_trades)
    metrics_long = calculate_metrics(long_trades)
    metrics_short = calculate_metrics(short_trades)

    # =========================
    # STRATEGY PERFORMANCE
    # =========================
    st.markdown("---")
    st.subheader("📊 Strategy Performance by Window")
    
    windows = [
        ("Today", filter_by_window(df_raw, days=0)),
        ("Yesterday", filter_by_window(df_raw, yesterday=True)),
        ("Last 3D", filter_by_window(df_raw, days=3)),
        ("Last 7D", filter_by_window(df_raw, days=7)),
        ("Last 30D", filter_by_window(df_raw, days=30)),
        ("Historical", df_raw),
    ]
    
    rows = []

    for label, window_df in windows:

        long_df = window_df[window_df["side"] == "LONG"]
        short_df = window_df[window_df["side"] == "SHORT"]

        metrics_long = calculate_metrics(long_df.to_dict("records"))
        metrics_short = calculate_metrics(short_df.to_dict("records"))
        
        long_exp = safe_metric(metrics_long, "expectancy")
        short_exp = safe_metric(metrics_short, "expectancy")

        edge_difference = round(long_exp - short_exp, 2)

        # Which side has edge?
        if long_exp > 0 and short_exp < 0:
            side_bias = "🟢 LONG EDGE"

        elif long_exp < 0 and short_exp > 0:
            side_bias = "🔴 SHORT EDGE"

        elif long_exp < 0 and short_exp < 0:
            side_bias = "⚪ NO EDGE"

        else:
            side_bias = "🟡 BOTH SIDES"

        # Recommendation
        if long_exp <= 0 and short_exp <= 0:
            recommendation = "⚪ NO TRADE"

        elif edge_difference > 0.10:
            recommendation = "🟢 LONG ONLY"

        elif edge_difference < -0.10:
            recommendation = "🔴 SHORT ONLY"

        else:
            recommendation = "🟡 BOTH"

        rows.append({
            "window": label,

            "long_trades": safe_metric(metrics_long, "trades"),
            "long_winrate": safe_metric(metrics_long, "winrate", True),
            "long_net_pnl": safe_metric(metrics_long, "net_pnl"),
            "long_expectancy": long_exp,

            "short_trades": safe_metric(metrics_short, "trades"),
            "short_winrate": safe_metric(metrics_short, "winrate", True),
            "short_net_pnl": safe_metric(metrics_short, "net_pnl"),
            "short_expectancy": short_exp,

            "edge_difference": edge_difference,
            "side_bias": side_bias,
            "recommendation": recommendation,
        })
        
    strategy_window_df = pd.DataFrame(rows)

    st.dataframe(
        strategy_window_df,
        use_container_width=True
    )
    
    st.markdown("### Today 4H Blocks")

    today_4h_df = build_today_4h_strategy_performance(df_raw)

    if today_4h_df.empty:
        st.info("No trades today for 4H block analysis.")
    else:
        st.dataframe(
            today_4h_df,
            use_container_width=True,
        )

    st.markdown("### 📊 Historical 4H Blocks")

    historical_4h_df = build_historical_4h_blocks(df_raw)

    if historical_4h_df.empty:
        st.info("No historical 4H block data.")
    else:
        st.dataframe(
            historical_4h_df,
            use_container_width=True,
        )
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
            
            
    # =========================
    # BTC DIRECTION MATRIX
    # =========================
    #st.markdown("---")
    #st.subheader("₿ BTC Direction Matrix")

    #btc_matrix = build_btc_direction_matrix(df_raw)

    #if btc_matrix.empty:
     #   st.info("Missing BTC direction columns or not enough data.")
    ##else:
      #  btc_pivot = build_btc_direction_pivot(btc_matrix)

       # st.markdown("### BTC 1H + BTC 15M: LONG vs SHORT")

        #st.dataframe(
           # btc_pivot.sort_values(["btc_direction_1h", "btc_direction_15m"]),
       #     use_container_width=True,
       # )

        #st.markdown("### Detailed Matrix")

       # st.dataframe(
        #    btc_matrix.sort_values(
         #       ["btc_direction_1h", "btc_direction_15m", "side"]
          #  ),
           # use_container_width=True,
        #)

        #st.caption(
         #   "Lectura rápida: BTC 1H UP + BTC 15M UP debería favorecer LONG. "
          #  "BTC 1H DOWN + BTC 15M DOWN debería favorecer SHORT. "
           # "Los regímenes mixtos suelen ser zonas de cuidado."
        #)

    # =========================
    # FORMAT DATES FOR DISPLAY ONLY
    # =========================
    df_display = df_view.copy()

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
            
        st.markdown("### Simulated Fixed TP")

        tp_levels = [0.20, 0.25, 0.30, 0.40, 0.50]

        sim_rows = []

        sim_df = df_view.copy()

        sim_df["pnl"] = pd.to_numeric(sim_df["pnl"], errors="coerce")
        sim_df["max_favorable_pct"] = pd.to_numeric(
            sim_df["max_favorable_pct"],
            errors="coerce"
        )

        sim_df = sim_df.dropna(subset=["pnl", "max_favorable_pct"])

        for tp_level in tp_levels:
            simulated_pnl = sim_df.apply(
                lambda row: tp_level
                if row["max_favorable_pct"] >= tp_level
                else row["pnl"],
                axis=1,
            )

            wins = simulated_pnl[simulated_pnl > 0].sum()
            losses = abs(simulated_pnl[simulated_pnl < 0].sum())

            sim_rows.append({
                "tp_level": f"{tp_level}%",
                "trades": len(simulated_pnl),
                "wins": int((simulated_pnl > 0).sum()),
                "losses": int((simulated_pnl <= 0).sum()),
                "winrate": round((simulated_pnl > 0).mean() * 100, 2),
                "avg_win": round(simulated_pnl[simulated_pnl > 0].mean(), 4),
                "avg_loss": round(simulated_pnl[simulated_pnl < 0].mean(), 4),
                "gross_win": round(simulated_pnl[simulated_pnl > 0].sum(), 4),
                "gross_loss": round(simulated_pnl[simulated_pnl < 0].sum(), 4),
                "avg_pnl": round(simulated_pnl.mean(), 4),
                "net_pnl": round(simulated_pnl.sum(), 4),
                "profit_factor": round(wins / losses, 2) if losses > 0 else None,
            })

        sim_tp_df = pd.DataFrame(sim_rows)

        st.dataframe(sim_tp_df, use_container_width=True)
        
        st.markdown("### Simulated Fixed TP / SL Matrix")

        tp_levels = [0.20, 0.25, 0.30, 0.40, 0.50]
        sl_levels = [0.30, 0.40, 0.50, 0.60, 0.80]

        matrix_rows = []

        matrix_df = df_view.copy()

        matrix_df["pnl"] = pd.to_numeric(matrix_df["pnl"], errors="coerce")
        matrix_df["max_favorable_pct"] = pd.to_numeric(matrix_df["max_favorable_pct"], errors="coerce")
        matrix_df["max_adverse_pct"] = pd.to_numeric(matrix_df["max_adverse_pct"], errors="coerce")

        matrix_df = matrix_df.dropna(subset=["pnl", "max_favorable_pct", "max_adverse_pct"])

        for tp_level in tp_levels:
            for sl_level in sl_levels:

                simulated = []

                for _, row in matrix_df.iterrows():

                    if row["max_favorable_pct"] >= tp_level:
                        simulated.append(tp_level)

                    elif abs(row["max_adverse_pct"]) >= sl_level:
                        simulated.append(-sl_level)

                    else:
                        simulated.append(row["pnl"])

                simulated_pnl = pd.Series(simulated)

                gross_win = simulated_pnl[simulated_pnl > 0].sum()
                gross_loss = abs(simulated_pnl[simulated_pnl < 0].sum())

                matrix_rows.append({
                    "tp": tp_level,
                    "sl": sl_level,
                    "trades": len(simulated_pnl),
                    "wins": int((simulated_pnl > 0).sum()),
                    "losses": int((simulated_pnl <= 0).sum()),
                    "winrate": round((simulated_pnl > 0).mean() * 100, 2),
                    "avg_pnl": round(simulated_pnl.mean(), 4),
                    "net_pnl": round(simulated_pnl.sum(), 4),
                    "pf": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
                })

        tp_sl_matrix_df = pd.DataFrame(matrix_rows)

        st.dataframe(
            tp_sl_matrix_df.sort_values(
                ["pf", "net_pnl"],
                ascending=False,
                na_position="last"
            ),
            use_container_width=True
        )
        
        st.markdown("### Pure TP / SL Matrix")

        pure_rows = []

        for tp_level in tp_levels:
            for sl_level in sl_levels:

                simulated = []

                for _, row in matrix_df.iterrows():
                    if row["max_favorable_pct"] >= tp_level:
                        simulated.append(tp_level)
                    else:
                        simulated.append(-sl_level)

                simulated_pnl = pd.Series(simulated)

                gross_win = simulated_pnl[simulated_pnl > 0].sum()
                gross_loss = abs(simulated_pnl[simulated_pnl < 0].sum())

                winrate = (simulated_pnl > 0).mean()
                avg_win = simulated_pnl[simulated_pnl > 0].mean()
                avg_loss = abs(simulated_pnl[simulated_pnl < 0].mean())

                expectancy = (winrate * avg_win) - ((1 - winrate) * avg_loss)

                pure_rows.append({
                    "tp": tp_level,
                    "sl": sl_level,
                    "trades": len(simulated_pnl),
                    "wins": int((simulated_pnl > 0).sum()),
                    "losses": int((simulated_pnl <= 0).sum()),
                    "winrate": round(winrate * 100, 2),
                    "avg_pnl": round(simulated_pnl.mean(), 4),
                    "net_pnl": round(simulated_pnl.sum(), 4),
                    "expectancy": round(expectancy, 4),
                    "pf": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
                })

        pure_matrix_df = pd.DataFrame(pure_rows)

        st.dataframe(
            pure_matrix_df.sort_values(
                ["expectancy", "pf", "net_pnl"],
                ascending=False,
                na_position="last"
            ),
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

    missing_cols = [c for c in required_cols if c not in df_view.columns]

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
            df_view
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
        
        st.markdown("### 🧭 Direction + Trend by Side")

        required_trend_cols = [
            "side",
            "signal_direction",
            "signal_trend",
            "pnl",
            "max_favorable_pct",
            "max_adverse_pct",
        ]

        missing_trend_cols = [c for c in required_trend_cols if c not in df_view.columns]

        if missing_trend_cols:
            st.info(f"Missing trend columns: {missing_trend_cols}")
        else:
            trend_df = (
                df_view
                .dropna(subset=["side", "signal_direction", "signal_trend"])
                .groupby(["side", "signal_direction", "signal_trend"])
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

            trend_df = trend_df[trend_df["trades"] >= min_trades]

            for col in ["avg_pnl", "total_pnl", "avg_mfe", "avg_mae"]:
                trend_df[col] = trend_df[col].round(3)

            trend_df = trend_df.sort_values(
                by=["pf", "total_pnl", "winrate"],
                ascending=False,
                na_position="last",
            )

            st.dataframe(trend_df, use_container_width=True)
            
        st.markdown("### 🔁 Momentum Previous Context")

        momentum_context_cols = [
            "side",
            "signal_momentum",
            "signal_momentum_prev1",
            "signal_momentum_prev2",
            "signal_momentum_sequence",
            "pnl",
            "max_favorable_pct",
            "max_adverse_pct",
        ]

        missing_mom_ctx_cols = [
            c for c in momentum_context_cols
            if c not in df_view.columns
        ]

        if missing_mom_ctx_cols:
            st.info(f"Missing momentum context columns: {missing_mom_ctx_cols}")

        else:

            def build_setup_table(group_cols):
                out = (
                    df_view
                    .dropna(subset=group_cols + ["side"])
                    .groupby(group_cols)
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

                out = out[out["trades"] >= min_trades]

                for col in ["avg_pnl", "total_pnl", "avg_mfe", "avg_mae"]:
                    out[col] = out[col].round(3)

                return out.sort_values(
                    by=["pf", "total_pnl", "winrate"],
                    ascending=False,
                    na_position="last",
                )

            st.markdown("#### Momentum + Prev1")

            mom_prev1_df = build_setup_table([
                "side",
                "signal_momentum_prev1",
                "signal_momentum",
            ])

            st.dataframe(mom_prev1_df, use_container_width=True)

            st.markdown("#### Momentum + Prev2 + Prev1")

            mom_prev2_df = build_setup_table([
                "side",
                "signal_momentum_prev2",
                "signal_momentum_prev1",
                "signal_momentum",
            ])

            st.dataframe(mom_prev2_df, use_container_width=True)

            st.markdown("#### Momentum Sequence")

            mom_seq_df = build_setup_table([
                "side",
                "signal_momentum_sequence",
            ])

            st.dataframe(mom_seq_df, use_container_width=True)
        
with tab_swings:

    st.markdown("---")
    st.subheader("🎯 Swing Context Analytics")

    required_cols = [
        "side",
        "pnl",
        "max_favorable_pct",
        "max_adverse_pct",
        "router_reason",
    ]

    missing_cols = [c for c in required_cols if c not in df_view.columns]

    if missing_cols:
        st.info(f"Missing columns: {missing_cols}")

    else:
        swing_df = df_view.copy().reset_index(drop=True)

        st.caption(f"Swings trades: {len(swing_df)}")

        swing_df["pnl"] = pd.to_numeric(swing_df["pnl"], errors="coerce")
        swing_df["max_favorable_pct"] = pd.to_numeric(
            swing_df["max_favorable_pct"],
            errors="coerce"
        )
        swing_df["max_adverse_pct"] = pd.to_numeric(
            swing_df["max_adverse_pct"],
            errors="coerce"
        )

        swing_df = swing_df.dropna(subset=["pnl"])

        def to_bool(series):
            return (
                series.astype(str)
                .str.lower()
                .isin(["true", "1", "yes"])
            )

        def swing_profit_factor(x):
            wins = x[x > 0].sum()
            losses = abs(x[x < 0].sum())
            return round(wins / losses, 2) if losses > 0 else None

        def swing_stats(name, data):
            if len(data) == 0:
                return None

            return {
                "setup": name,
                "trades": len(data),
                "wins": int((data["pnl"] > 0).sum()),
                "losses": int((data["pnl"] <= 0).sum()),
                "winrate": round((data["pnl"] > 0).mean() * 100, 2),
                "avg_return": round(data["pnl"].mean(), 4),
                "total_return": round(data["pnl"].sum(), 4),
                "avg_mfe": round(data["max_favorable_pct"].mean(), 4),
                "avg_mae": round(data["max_adverse_pct"].mean(), 4),
                "profit_factor": swing_profit_factor(data["pnl"]),
            }

        min_trades_swings = st.slider(
            "Minimum trades per swing setup",
            min_value=1,
            max_value=50,
            value=10,
            step=1,
            key="swings_min_trades",
        )

        # =========================
        # NEAR SWING STATS
        # =========================

        st.markdown("### Near Swing Stats")

        near_results = []

        for tf in ["15m", "1h", "4h"]:
            for side in ["LONG", "SHORT"]:
                for ref in ["low", "high"]:
                    col = f"near_swing_{ref}_{tf}"

                    if col not in swing_df.columns:
                        continue

                    subset = swing_df[
                        (swing_df["side"] == side)
                        & to_bool(swing_df[col])
                    ]

                    row = swing_stats(
                        f"{side} near swing {ref} {tf}",
                        subset
                    )

                    if row:
                        near_results.append(row)

        near_df = pd.DataFrame(near_results)

        if near_df.empty:
            st.info("No near swing data available.")
        else:
            near_df = near_df[near_df["trades"] >= min_trades_swings]

            near_df = near_df.sort_values(
                ["profit_factor", "trades"],
                ascending=[False, False],
                na_position="last",
            )

            st.dataframe(near_df, use_container_width=True)

        # =========================
        # DISTANCE BUCKETS
        # =========================

        st.markdown("### Distance Bucket Stats")

        BUCKETS = [-999, -4, -2, -1, 0, 1, 2, 4, 8, 999]

        LABELS = [
            "< -4%",
            "-4% to -2%",
            "-2% to -1%",
            "-1% to 0%",
            "0% to 1%",
            "1% to 2%",
            "2% to 4%",
            "4% to 8%",
            "> 8%",
        ]

        distance_results = []

        for tf in ["15m", "1h", "4h"]:
            for side in ["LONG", "SHORT"]:
                for ref in ["low", "high"]:
                    col = f"dist_swing_{ref}_{tf}_pct"

                    if col not in swing_df.columns:
                        continue

                    temp = swing_df[swing_df["side"] == side].copy()
                    temp[col] = pd.to_numeric(temp[col], errors="coerce")
                    temp = temp.dropna(subset=[col, "pnl"])

                    if temp.empty:
                        continue

                    temp["bucket"] = pd.cut(
                        temp[col],
                        bins=BUCKETS,
                        labels=LABELS,
                        include_lowest=True,
                    )

                    for bucket, group in temp.groupby("bucket", observed=False):
                        if len(group) == 0:
                            continue

                        row = swing_stats(
                            f"{side} dist swing {ref} {tf} {bucket}",
                            group
                        )

                        if row:
                            row["side"] = side
                            row["tf"] = tf
                            row["reference"] = ref
                            row["bucket"] = str(bucket)
                            distance_results.append(row)

        distance_df = pd.DataFrame(distance_results)

        if distance_df.empty:
            st.info("No distance bucket data available.")
        else:
            distance_filtered = distance_df[
                distance_df["trades"] >= min_trades_swings
            ]

            best_distance = distance_filtered.sort_values(
                ["profit_factor", "trades"],
                ascending=[False, False],
                na_position="last",
            )

            worst_distance = distance_filtered.sort_values(
                ["profit_factor", "avg_return"],
                ascending=[True, True],
                na_position="last",
            )

            col_a, col_b = st.columns(2)

            with col_a:
                st.markdown("#### Best Swing Buckets")
                st.dataframe(best_distance, use_container_width=True)

            with col_b:
                st.markdown("#### Worst Swing Buckets")
                st.dataframe(worst_distance, use_container_width=True)

        # =========================
        # ROUTER x SWING
        # =========================

        st.markdown("### Router Reason × Swing Distance")

        router_results = []

        if "router_reason" not in swing_df.columns:
            st.info("router_reason column not found.")

        else:
            for reason in swing_df["router_reason"].dropna().unique():
                for side in ["LONG", "SHORT"]:
                    for tf in ["15m", "1h", "4h"]:
                        for ref in ["low", "high"]:
                            col = f"dist_swing_{ref}_{tf}_pct"

                            if col not in swing_df.columns:
                                continue

                            temp = swing_df[
                                (swing_df["router_reason"] == reason)
                                & (swing_df["side"] == side)
                            ].copy()

                            temp[col] = pd.to_numeric(temp[col], errors="coerce")
                            temp = temp.dropna(subset=[col, "pnl"])

                            if temp.empty:
                                continue

                            temp["bucket"] = pd.cut(
                                temp[col],
                                bins=BUCKETS,
                                labels=LABELS,
                                include_lowest=True,
                            )

                            for bucket, group in temp.groupby("bucket", observed=False):
                                if len(group) < min_trades_swings:
                                    continue

                                row = swing_stats(
                                    f"{reason} | {side} | {ref} {tf} | {bucket}",
                                    group
                                )

                                if row:
                                    row["reason"] = reason
                                    row["side"] = side
                                    row["tf"] = tf
                                    row["reference"] = ref
                                    row["bucket"] = str(bucket)
                                    router_results.append(row)

            router_df = pd.DataFrame(router_results)

            if router_df.empty:
                st.info("No router × swing groups with enough trades.")
            else:
                router_best = router_df.sort_values(
                    ["profit_factor", "trades"],
                    ascending=[False, False],
                    na_position="last",
                )

                router_worst = router_df.sort_values(
                    ["profit_factor", "avg_return"],
                    ascending=[True, True],
                    na_position="last",
                )

                col_c, col_d = st.columns(2)

                with col_c:
                    st.markdown("#### Best Router × Swing")
                    st.dataframe(router_best, use_container_width=True)

                with col_d:
                    st.markdown("#### Worst Router × Swing")
                    st.dataframe(router_worst, use_container_width=True)
                    
        # =========================
        # SWING × SWING CROSS
        # =========================

        st.markdown("### Swing × Swing Cross")

        st.caption(
            "Cruza distancia a soporte vs distancia a resistencia. "
            "Ej: LONG low 15m 4%-8% + high 4h >8%."
        )

        cross_results = []

        swing_cross_pairs = [
            # LONG: soporte cercano/medio vs espacio a resistencia
            ("LONG", "low", "15m", "high", "4h"),
            ("LONG", "low", "1h", "high", "4h"),
            ("LONG", "low", "15m", "high", "1h"),

            # SHORT: resistencia vs espacio a soporte
            ("SHORT", "high", "15m", "low", "4h"),
            ("SHORT", "high", "1h", "low", "4h"),
            ("SHORT", "high", "15m", "low", "1h"),
        ]

        for side, ref_a, tf_a, ref_b, tf_b in swing_cross_pairs:
            col_a = f"dist_swing_{ref_a}_{tf_a}_pct"
            col_b = f"dist_swing_{ref_b}_{tf_b}_pct"

            if col_a not in swing_df.columns or col_b not in swing_df.columns:
                continue

            temp = swing_df[swing_df["side"] == side].copy()

            temp[col_a] = pd.to_numeric(temp[col_a], errors="coerce")
            temp[col_b] = pd.to_numeric(temp[col_b], errors="coerce")

            temp = temp.dropna(subset=[col_a, col_b, "pnl"])

            if temp.empty:
                continue

            temp["bucket_a"] = pd.cut(
                temp[col_a],
                bins=BUCKETS,
                labels=LABELS,
                include_lowest=True,
            )

            temp["bucket_b"] = pd.cut(
                temp[col_b],
                bins=BUCKETS,
                labels=LABELS,
                include_lowest=True,
            )

            for (bucket_a, bucket_b), group in temp.groupby(
                ["bucket_a", "bucket_b"],
                observed=False
            ):
                if len(group) < min_trades_swings:
                    continue

                setup_name = (
                    f"{side} | "
                    f"{ref_a} {tf_a} {bucket_a} | "
                    f"{ref_b} {tf_b} {bucket_b}"
                )

                row = swing_stats(setup_name, group)

                if row:
                    row["side"] = side
                    row["ref_a"] = ref_a
                    row["tf_a"] = tf_a
                    row["bucket_a"] = str(bucket_a)
                    row["ref_b"] = ref_b
                    row["tf_b"] = tf_b
                    row["bucket_b"] = str(bucket_b)
                    cross_results.append(row)

        cross_df = pd.DataFrame(cross_results)

        if cross_df.empty:
            st.info("No swing × swing cross groups with enough trades.")
        else:
            cross_best = cross_df.sort_values(
                ["profit_factor", "trades"],
                ascending=[False, False],
                na_position="last",
            )

            cross_worst = cross_df.sort_values(
                ["profit_factor", "avg_return"],
                ascending=[True, True],
                na_position="last",
            )

            col_e, col_f = st.columns(2)

            with col_e:
                st.markdown("#### Best Swing × Swing")
                st.dataframe(cross_best, use_container_width=True)

            with col_f:
                st.markdown("#### Worst Swing × Swing")
                st.dataframe(cross_worst, use_container_width=True)
                
        # =========================
        # ROUTER × SWING × SWING CROSS
        # =========================

        st.markdown("### Router Reason × Swing × Swing Cross")

        router_cross_results = []

        if "router_reason" not in swing_df.columns:
            st.info("router_reason column not found.")
        else:
            for reason in swing_df["router_reason"].dropna().unique():

                for side, ref_a, tf_a, ref_b, tf_b in swing_cross_pairs:
                    col_a = f"dist_swing_{ref_a}_{tf_a}_pct"
                    col_b = f"dist_swing_{ref_b}_{tf_b}_pct"

                    if col_a not in swing_df.columns or col_b not in swing_df.columns:
                        continue

                    temp = swing_df[
                        (swing_df["side"] == side)
                        & (swing_df["router_reason"] == reason)
                    ].copy()

                    temp[col_a] = pd.to_numeric(temp[col_a], errors="coerce")
                    temp[col_b] = pd.to_numeric(temp[col_b], errors="coerce")

                    temp = temp.dropna(subset=[col_a, col_b, "pnl"])

                    if temp.empty:
                        continue

                    temp["bucket_a"] = pd.cut(
                        temp[col_a],
                        bins=BUCKETS,
                        labels=LABELS,
                        include_lowest=True,
                    )

                    temp["bucket_b"] = pd.cut(
                        temp[col_b],
                        bins=BUCKETS,
                        labels=LABELS,
                        include_lowest=True,
                    )

                    for (bucket_a, bucket_b), group in temp.groupby(
                        ["bucket_a", "bucket_b"],
                        observed=False,
                    ):
                        if len(group) < min_trades_swings:
                            continue

                        setup_name = (
                            f"{reason} | {side} | "
                            f"{ref_a} {tf_a} {bucket_a} | "
                            f"{ref_b} {tf_b} {bucket_b}"
                        )

                        row = swing_stats(setup_name, group)

                        if row:
                            row["reason"] = reason
                            row["side"] = side
                            row["ref_a"] = ref_a
                            row["tf_a"] = tf_a
                            row["bucket_a"] = str(bucket_a)
                            row["ref_b"] = ref_b
                            row["tf_b"] = tf_b
                            row["bucket_b"] = str(bucket_b)
                            router_cross_results.append(row)

            router_cross_df = pd.DataFrame(router_cross_results)

            if router_cross_df.empty:
                st.info("No router × swing × swing groups with enough trades.")
            else:
                router_cross_best = router_cross_df.sort_values(
                    ["profit_factor", "trades"],
                    ascending=[False, False],
                    na_position="last",
                )

                router_cross_worst = router_cross_df.sort_values(
                    ["profit_factor", "avg_return"],
                    ascending=[True, True],
                    na_position="last",
                )

                col_g, col_h = st.columns(2)

                with col_g:
                    st.markdown("#### Best Router × Swing × Swing")
                    st.dataframe(router_cross_best, use_container_width=True)

                with col_h:
                    st.markdown("#### Worst Router × Swing × Swing")
                    st.dataframe(router_cross_worst, use_container_width=True)

        # =========================
        # MOMENTUM × SWING × SWING CROSS
        # =========================

        st.markdown("### Momentum × Swing × Swing Cross")

        momentum_col = next(
            (
                c for c in [
                    "signal_momentum",
                    "momentum",
                    "current_momentum",
                    "entry_momentum",
                ]
                if c in swing_df.columns
            ),
            None,
        )

        min_trades_momentum_cross = st.slider(
            "Minimum trades per momentum swing cross",
            min_value=2,
            max_value=30,
            value=5,
            step=1,
            key="momentum_swing_cross_min_trades",
        )

        if momentum_col is None:
            st.info("No momentum column found.")
        else:
            mom_df = swing_df.copy()

            mom_df["_momentum"] = (
                mom_df[momentum_col]
                .astype(str)
                .str.strip()
                .str.lower()
            )

            mom_rows = []

            for side, ref_a, tf_a, ref_b, tf_b in swing_cross_pairs:
                col_a = f"dist_swing_{ref_a}_{tf_a}_pct"
                col_b = f"dist_swing_{ref_b}_{tf_b}_pct"

                if col_a not in mom_df.columns or col_b not in mom_df.columns:
                    continue

                temp = mom_df[mom_df["side"] == side].copy()

                temp[col_a] = pd.to_numeric(temp[col_a], errors="coerce")
                temp[col_b] = pd.to_numeric(temp[col_b], errors="coerce")

                temp = temp.dropna(subset=[col_a, col_b, "pnl", "_momentum"])

                if temp.empty:
                    continue

                temp["bucket_a"] = pd.cut(
                    temp[col_a],
                    bins=BUCKETS,
                    labels=LABELS,
                    include_lowest=True,
                )

                temp["bucket_b"] = pd.cut(
                    temp[col_b],
                    bins=BUCKETS,
                    labels=LABELS,
                    include_lowest=True,
                )

                group_cols = [
                    "_momentum",
                    "side",
                    "bucket_a",
                    "bucket_b",
                ]

                if "router_reason" in temp.columns:
                    group_cols = ["router_reason"] + group_cols

                grouped = temp.groupby(group_cols, observed=False)

                for keys, group in grouped:
                    if len(group) < min_trades_momentum_cross:
                        continue

                    if "router_reason" in temp.columns:
                        reason, momentum, side_key, bucket_a, bucket_b = keys
                    else:
                        reason = "NO_REASON"
                        momentum, side_key, bucket_a, bucket_b = keys

                    setup_name = (
                        f"{reason} | {momentum} | {side_key} | "
                        f"{ref_a} {tf_a} {bucket_a} | "
                        f"{ref_b} {tf_b} {bucket_b}"
                    )

                    row = swing_stats(setup_name, group)

                    if row:
                        row["reason"] = reason
                        row["momentum"] = momentum
                        row["side"] = side_key
                        row["ref_a"] = ref_a
                        row["tf_a"] = tf_a
                        row["bucket_a"] = str(bucket_a)
                        row["ref_b"] = ref_b
                        row["tf_b"] = tf_b
                        row["bucket_b"] = str(bucket_b)
                        mom_rows.append(row)

            mom_cross_df = pd.DataFrame(mom_rows)

            if mom_cross_df.empty:
                st.info("No momentum × swing × swing groups with enough trades.")
            else:
                mom_best = mom_cross_df.sort_values(
                    ["profit_factor", "trades"],
                    ascending=[False, False],
                    na_position="last",
                )

                mom_worst = mom_cross_df.sort_values(
                    ["profit_factor", "avg_return"],
                    ascending=[True, True],
                    na_position="last",
                )

                col_i, col_j = st.columns(2)

                with col_i:
                    st.markdown("#### Best Momentum × Swing × Swing")
                    st.dataframe(mom_best, use_container_width=True)

                with col_j:
                    st.markdown("#### Worst Momentum × Swing × Swing")
                    st.dataframe(mom_worst, use_container_width=True)


        # =========================
        # SPACE ANALYSIS
        # =========================

        st.markdown("### Swing Space Analysis")

        st.caption(
            "Mide el espacio entre swing low y swing high del mismo timeframe. "
            "Para LONG interesa espacio hacia arriba; para SHORT espacio hacia abajo."
        )

        SPACE_BUCKETS = [-999, 0, 1, 2, 4, 8, 999]

        SPACE_LABELS = [
            "< 0%",
            "0% to 1%",
            "1% to 2%",
            "2% to 4%",
            "4% to 8%",
            "> 8%",
        ]

        space_df = swing_df.copy()

        space_results = []
        router_space_results = []

        for tf in ["15m", "1h", "4h"]:
            low_col = f"dist_swing_low_{tf}_pct"
            high_col = f"dist_swing_high_{tf}_pct"

            if low_col not in space_df.columns or high_col not in space_df.columns:
                continue

            space_df[low_col] = pd.to_numeric(space_df[low_col], errors="coerce")
            space_df[high_col] = pd.to_numeric(space_df[high_col], errors="coerce")

            space_col = f"swing_space_{tf}_pct"

            space_df[space_col] = space_df[high_col] + space_df[low_col]

            temp = space_df.dropna(subset=[space_col, "pnl"]).copy()

            temp["space_bucket"] = pd.cut(
                temp[space_col],
                bins=SPACE_BUCKETS,
                labels=SPACE_LABELS,
                include_lowest=True,
            )

            for side in ["LONG", "SHORT"]:
                side_temp = temp[temp["side"] == side]

                for bucket, group in side_temp.groupby("space_bucket", observed=False):
                    if len(group) < min_trades_swings:
                        continue

                    row = swing_stats(
                        f"{side} space {tf} {bucket}",
                        group
                    )

                    if row:
                        row["side"] = side
                        row["tf"] = tf
                        row["space_bucket"] = str(bucket)
                        row["avg_space"] = round(group[space_col].mean(), 4)
                        space_results.append(row)

                if "router_reason" in temp.columns:
                    for reason in temp["router_reason"].dropna().unique():
                        reason_temp = side_temp[
                            side_temp["router_reason"] == reason
                        ]

                        for bucket, group in reason_temp.groupby("space_bucket", observed=False):
                            if len(group) < min_trades_swings:
                                continue

                            row = swing_stats(
                                f"{reason} | {side} | space {tf} {bucket}",
                                group
                            )

                            if row:
                                row["reason"] = reason
                                row["side"] = side
                                row["tf"] = tf
                                row["space_bucket"] = str(bucket)
                                row["avg_space"] = round(group[space_col].mean(), 4)
                                router_space_results.append(row)

        space_stats_df = pd.DataFrame(space_results)
        router_space_df = pd.DataFrame(router_space_results)

        if space_stats_df.empty:
            st.info("No swing space data available.")
        else:
            space_best = space_stats_df.sort_values(
                ["profit_factor", "trades"],
                ascending=[False, False],
                na_position="last",
            )

            space_worst = space_stats_df.sort_values(
                ["profit_factor", "avg_return"],
                ascending=[True, True],
                na_position="last",
            )

            col_k, col_l = st.columns(2)

            with col_k:
                st.markdown("#### Best Space Buckets")
                st.dataframe(space_best, use_container_width=True)

            with col_l:
                st.markdown("#### Worst Space Buckets")
                st.dataframe(space_worst, use_container_width=True)

        if not router_space_df.empty:
            router_space_best = router_space_df.sort_values(
                ["profit_factor", "trades"],
                ascending=[False, False],
                na_position="last",
            )

            router_space_worst = router_space_df.sort_values(
                ["profit_factor", "avg_return"],
                ascending=[True, True],
                na_position="last",
            )

            col_m, col_n = st.columns(2)

            with col_m:
                st.markdown("#### Best Router × Space")
                st.dataframe(router_space_best, use_container_width=True)

            with col_n:
                st.markdown("#### Worst Router × Space")
                st.dataframe(router_space_worst, use_container_width=True)
        
        # =========================
        # LONG WHITELIST IMPACT
        # =========================

        st.markdown("### Long Whitelist Impact + Poison Block")

        required_long_cols = [
            "dist_swing_low_15m_pct",
            "dist_swing_high_15m_pct",
            "dist_swing_high_4h_pct",
            "pnl",
            "max_favorable_pct",
            "max_adverse_pct",
        ]

        missing_long_cols = [c for c in required_long_cols if c not in swing_df.columns]

        if missing_long_cols:
            st.info(f"Missing long whitelist columns: {missing_long_cols}")
        else:
            long_df = swing_df[swing_df["side"] == "LONG"].copy()

            long_df["dist_swing_low_15m_pct"] = pd.to_numeric(
                long_df["dist_swing_low_15m_pct"], errors="coerce"
            )
            long_df["dist_swing_high_15m_pct"] = pd.to_numeric(
                long_df["dist_swing_high_15m_pct"], errors="coerce"
            )
            long_df["dist_swing_high_4h_pct"] = pd.to_numeric(
                long_df["dist_swing_high_4h_pct"], errors="coerce"
            )

            # Poison block:
            # LONG dist swing high 4h 0%-1%
            long_df["_poison_high_4h_0_1"] = (
                long_df["dist_swing_high_4h_pct"].notna()
                & (long_df["dist_swing_high_4h_pct"] >= 0)
                & (long_df["dist_swing_high_4h_pct"] < 1)
            )

            long_rules = [
                {
                    "setup": "long_high_15m_1_2",
                    "mask": (
                        long_df["dist_swing_high_15m_pct"].notna()
                        & (long_df["dist_swing_high_15m_pct"] >= 1)
                        & (long_df["dist_swing_high_15m_pct"] < 2)
                    ),
                },
                {
                    "setup": "long_low_15m_4_8",
                    "mask": (
                        long_df["dist_swing_low_15m_pct"].notna()
                        & (long_df["dist_swing_low_15m_pct"] >= 4)
                        & (long_df["dist_swing_low_15m_pct"] < 8)
                    ),
                },
                {
                    "setup": "long_high_4h_gt_8",
                    "mask": (
                        long_df["dist_swing_high_4h_pct"].notna()
                        & (long_df["dist_swing_high_4h_pct"] >= 8)
                    ),
                },
            ]

            impact_rows = []

            for rule in long_rules:
                setup_name = rule["setup"]
                base_mask = rule["mask"]

                original = long_df[base_mask].copy()
                blocked = long_df[base_mask & long_df["_poison_high_4h_0_1"]].copy()
                after_block = long_df[base_mask & ~long_df["_poison_high_4h_0_1"]].copy()

                original_stats = swing_stats(f"{setup_name} BEFORE block", original)
                after_stats = swing_stats(f"{setup_name} AFTER block", after_block)

                if original_stats:
                    impact_rows.append({
                        "setup": setup_name,
                        "status": "before_block",
                        "blocked_trades": len(blocked),
                        "remaining_trades": len(after_block),
                        **original_stats,
                    })

                if after_stats:
                    impact_rows.append({
                        "setup": setup_name,
                        "status": "after_block",
                        "blocked_trades": len(blocked),
                        "remaining_trades": len(after_block),
                        **after_stats,
                    })

            impact_df = pd.DataFrame(impact_rows)

            if impact_df.empty:
                st.info("No long whitelist impact data available.")
            else:
                cols_order = [
                    "setup",
                    "status",
                    "trades",
                    "blocked_trades",
                    "remaining_trades",
                    "wins",
                    "losses",
                    "winrate",
                    "avg_return",
                    "total_return",
                    "avg_mfe",
                    "avg_mae",
                    "profit_factor",
                ]

                impact_df = impact_df[[c for c in cols_order if c in impact_df.columns]]

                st.dataframe(
                    impact_df.sort_values(
                        ["setup", "status"],
                        ascending=[True, True],
                    ),
                    use_container_width=True,
                )

            # =========================
            # Combined whitelist impact
            # =========================

            combined_allowed = False

            for rule in long_rules:
                combined_allowed = combined_allowed | rule["mask"]

            combined_original = long_df[combined_allowed].copy()
            combined_blocked = long_df[
                combined_allowed & long_df["_poison_high_4h_0_1"]
            ].copy()
            combined_after = long_df[
                combined_allowed & ~long_df["_poison_high_4h_0_1"]
            ].copy()

            combined_rows = []

            original_stats = swing_stats(
                "ALL_LONG_WHITELIST BEFORE block",
                combined_original,
            )

            after_stats = swing_stats(
                "ALL_LONG_WHITELIST AFTER block",
                combined_after,
            )

            if original_stats:
                combined_rows.append({
                    "setup": "ALL_LONG_WHITELIST",
                    "status": "before_block",
                    "blocked_trades": len(combined_blocked),
                    "remaining_trades": len(combined_after),
                    **original_stats,
                })

            if after_stats:
                combined_rows.append({
                    "setup": "ALL_LONG_WHITELIST",
                    "status": "after_block",
                    "blocked_trades": len(combined_blocked),
                    "remaining_trades": len(combined_after),
                    **after_stats,
                })

            combined_df = pd.DataFrame(combined_rows)

            if not combined_df.empty:
                st.markdown("#### Combined LONG Whitelist Impact")

                combined_df = combined_df[
                    [c for c in cols_order if c in combined_df.columns]
                ]

                st.dataframe(combined_df, use_container_width=True)
                
                
        # =========================
        # SETUP LOSER DIAGNOSTICS
        # =========================

        st.markdown("### Setup Winner vs Loser Diagnostics")

        diagnostic_setup = st.selectbox(
            "Diagnostic setup",
            [
                "long_low_15m_4_8",
                "long_high_15m_1_2",
                "long_high_4h_gt_8",
                "short_near_swing_high_4h",
            ],
            key="diagnostic_setup_select",
        )

        diag_df = swing_df.copy()

        for c in [
            "dist_swing_low_15m_pct",
            "dist_swing_high_15m_pct",
            "dist_swing_high_4h_pct",
            "dist_swing_low_4h_pct",
            "move_5_bars_pct",
            "move_10_bars_pct",
            "green_candles_last_10",
            "red_candles_last_10",
            "relative_volume_15m",
            "relative_volume_1h",
            "relative_volume_4h",
            "btc_velocity_15m",
            "btc_velocity_1h",
        ]:
            if c in diag_df.columns:
                diag_df[c] = pd.to_numeric(diag_df[c], errors="coerce")

        if diagnostic_setup == "long_low_15m_4_8":
            setup_mask = (
                (diag_df["side"] == "LONG")
                & (diag_df["router_reason"].isin(["range_breakout_up", "long_low_15m_4_8"]))
                & (diag_df["dist_swing_low_15m_pct"] >= 4)
                & (diag_df["dist_swing_low_15m_pct"] < 8)
            )

        elif diagnostic_setup == "long_high_15m_1_2":
            setup_mask = (
                (diag_df["side"] == "LONG")
                & (diag_df["dist_swing_high_15m_pct"] >= 1)
                & (diag_df["dist_swing_high_15m_pct"] < 2)
            )

        elif diagnostic_setup == "long_high_4h_gt_8":
            setup_mask = (
                (diag_df["side"] == "LONG")
                & (diag_df["dist_swing_high_4h_pct"] >= 8)
            )

        else:
            setup_mask = (
                (diag_df["side"] == "SHORT")
                & (
                    diag_df["near_swing_high_4h"]
                    .astype(str)
                    .str.lower()
                    .isin(["true", "1", "yes"])
                )
            )

        setup_diag = diag_df[setup_mask].copy()

        if setup_diag.empty:
            st.info("No trades found for selected diagnostic setup.")
        else:
            setup_diag["result"] = setup_diag["pnl"].apply(
                lambda x: "WIN" if x > 0 else "LOSS"
            )

            numeric_cols = [
                "pnl",
                "max_favorable_pct",
                "max_adverse_pct",
                "dist_swing_low_15m_pct",
                "dist_swing_high_15m_pct",
                "dist_swing_low_1h_pct",
                "dist_swing_high_1h_pct",
                "dist_swing_low_4h_pct",
                "dist_swing_high_4h_pct",
                "move_5_bars_pct",
                "move_10_bars_pct",
                "green_candles_last_10",
                "red_candles_last_10",
                "relative_volume_15m",
                "relative_volume_1h",
                "relative_volume_4h",
                "btc_velocity_15m",
                "btc_velocity_1h",
            ]

            numeric_cols = [c for c in numeric_cols if c in setup_diag.columns]

            comparison = (
                setup_diag
                .groupby("result")[numeric_cols]
                .mean()
                .round(4)
                .reset_index()
            )

            st.markdown("#### Winners vs Losers — Numeric Averages")
            st.dataframe(comparison, use_container_width=True)

            categorical_cols = [
                "signal_momentum",
                "btc_direction_15m",
                "btc_direction_1h",
                "btc_context_state",
                "volume_tier",
                "rvol_tier_15m",
                "rvol_tier_1h",
                "rvol_tier_4h",
            ]

            categorical_cols = [c for c in categorical_cols if c in setup_diag.columns]

            cat_rows = []

            for col in categorical_cols:
                pivot = (
                    setup_diag
                    .groupby([col, "result"])
                    .size()
                    .unstack(fill_value=0)
                    .reset_index()
                )

                if "WIN" not in pivot.columns:
                    pivot["WIN"] = 0
                if "LOSS" not in pivot.columns:
                    pivot["LOSS"] = 0

                pivot["total"] = pivot["WIN"] + pivot["LOSS"]
                pivot["winrate"] = (pivot["WIN"] / pivot["total"] * 100).round(2)
                pivot["feature"] = col

                pivot = pivot.rename(columns={col: "value"})

                cat_rows.append(pivot[["feature", "value", "total", "WIN", "LOSS", "winrate"]])

            if cat_rows:
                categorical_diag = pd.concat(cat_rows, ignore_index=True)
                categorical_diag = categorical_diag.sort_values(
                    ["feature", "total"],
                    ascending=[True, False],
                )

                st.markdown("#### Winners vs Losers — Categorical Breakdown")
                st.dataframe(categorical_diag, use_container_width=True)

            show_cols = [
                "symbol",
                "side",
                "entry_ts",
                "exit_ts",
                "pnl",
                "exit_reason",
                "signal_momentum",
                "btc_context_state",
                "btc_direction_15m",
                "btc_direction_1h",
                "dist_swing_low_15m_pct",
                "dist_swing_high_15m_pct",
                "dist_swing_high_4h_pct",
                "relative_volume_15m",
                "move_5_bars_pct",
                "move_10_bars_pct",
                "green_candles_last_10",
                "red_candles_last_10",
            ]

            show_cols = [c for c in show_cols if c in setup_diag.columns]

            st.markdown("#### Raw Trades")
            st.dataframe(
                setup_diag[show_cols].sort_values("pnl"),
                use_container_width=True,
            )
            
        # =========================
        # WINNERS ONLY
        # =========================

        st.markdown("#### Winners Only")

        winners = setup_diag[setup_diag["pnl"] > 0].copy()

        winners_cols = [
            "symbol",
            "pnl",
            "exit_reason",

            "signal_momentum",
            "signal_momentum_prev1",
            "signal_momentum_prev2",
            "signal_momentum_sequence",

            "btc_context_state",
            "btc_direction_15m",
            "btc_direction_1h",
            "btc_velocity_15m",
            "btc_velocity_1h",

            "volume_tier",
            "rvol_tier_15m",
            "rvol_tier_1h",
            "rvol_tier_4h",

            "move_5_bars_pct",
            "move_10_bars_pct",
            "green_candles_last_10",
            "red_candles_last_10",

            "dist_swing_low_15m_pct",
            "dist_swing_high_15m_pct",

            "dist_swing_low_1h_pct",
            "dist_swing_high_1h_pct",

            "dist_swing_low_4h_pct",
            "dist_swing_high_4h_pct",

            "max_favorable_pct",
            "max_adverse_pct",
        ]

        winners_cols = [c for c in winners_cols if c in winners.columns]

        st.dataframe(
            winners[winners_cols]
            .sort_values("pnl", ascending=False),
            use_container_width=True
        )
        
                    
        # =========================
        # LOSERS ONLY
        # =========================

        st.markdown("#### Losers Only")

        losers = setup_diag[setup_diag["pnl"] <= 0].copy()

        losers_cols = [
            "symbol",
            "pnl",
            "exit_reason",

            "signal_momentum",
            "signal_momentum_prev1",
            "signal_momentum_prev2",
            "signal_momentum_sequence",

            "btc_context_state",
            "btc_direction_15m",
            "btc_direction_1h",
            "btc_velocity_15m",
            "btc_velocity_1h",

            "volume_tier",
            "rvol_tier_15m",
            "rvol_tier_1h",
            "rvol_tier_4h",

            "move_5_bars_pct",
            "move_10_bars_pct",
            "green_candles_last_10",
            "red_candles_last_10",

            "dist_swing_low_15m_pct",
            "dist_swing_high_15m_pct",

            "dist_swing_low_1h_pct",
            "dist_swing_high_1h_pct",

            "dist_swing_low_4h_pct",
            "dist_swing_high_4h_pct",

            "max_favorable_pct",
            "max_adverse_pct",
        ]

        losers_cols = [c for c in losers_cols if c in losers.columns]

        st.dataframe(
            losers[losers_cols]
            .sort_values("pnl"),
            use_container_width=True
        )
        
        # =========================
        # MOVE_10_BARS BUCKETS
        # =========================

        st.markdown("#### Move 10 Bars Buckets")

        if "move_10_bars_pct" in setup_diag.columns:

            temp = setup_diag.copy()

            temp["move_10_bars_pct"] = pd.to_numeric(
                temp["move_10_bars_pct"],
                errors="coerce"
            )

            MOVE_BINS = [-999, 0, 2, 5, 10, 20, 999]

            MOVE_LABELS = [
                "<0",
                "0-2",
                "2-5",
                "5-10",
                "10-20",
                ">20"
            ]

            temp["move10_bucket"] = pd.cut(
                temp["move_10_bars_pct"],
                bins=MOVE_BINS,
                labels=MOVE_LABELS,
                include_lowest=True,
            )

            rows = []

            for bucket, group in temp.groupby("move10_bucket", observed=False):

                if len(group) == 0:
                    continue

                rows.append(
                    swing_stats(
                        str(bucket),
                        group
                    )
                )

            move10_df = pd.DataFrame(rows)

            if not move10_df.empty:
                move10_df = move10_df.sort_values(
                    "profit_factor",
                    ascending=False,
                    na_position="last"
                )

                st.dataframe(
                    move10_df,
                    use_container_width=True
                )
                
                
        # =========================
        # MOVE10 × GREEN CANDLES
        # =========================

        st.markdown("#### Move10 × Green Candles")

        if (
            "move_10_bars_pct" in setup_diag.columns
            and "green_candles_last_10" in setup_diag.columns
        ):

            temp = setup_diag.copy()

            temp["move_10_bars_pct"] = pd.to_numeric(
                temp["move_10_bars_pct"],
                errors="coerce"
            )

            temp["green_candles_last_10"] = pd.to_numeric(
                temp["green_candles_last_10"],
                errors="coerce"
            )

            # =========================
            # Move10 buckets
            # =========================
            MOVE_BINS = [-999, 0, 2, 5, 10, 20, 999]

            MOVE_LABELS = [
                "<0",
                "0-2",
                "2-5",
                "5-10",
                "10-20",
                ">20"
            ]

            temp["move10_bucket"] = pd.cut(
                temp["move_10_bars_pct"],
                bins=MOVE_BINS,
                labels=MOVE_LABELS,
                include_lowest=True,
            )

            rows = []

            for move_bucket, move_group in temp.groupby(
                "move10_bucket",
                observed=False
            ):

                if len(move_group) == 0:
                    continue

                for greens, group in move_group.groupby(
                    "green_candles_last_10",
                    observed=False
                ):

                    if len(group) == 0:
                        continue

                    row = swing_stats(
                        f"{move_bucket} | {int(greens)} greens",
                        group
                    )

                    if row:
                        row["move10_bucket"] = str(move_bucket)
                        row["greens"] = int(greens)

                        rows.append(row)

            combo_df = pd.DataFrame(rows)

            if combo_df.empty:
                st.info("No Move10 × Green data.")
            else:

                combo_df = combo_df.sort_values(
                    ["profit_factor", "trades"],
                    ascending=[False, False],
                    na_position="last"
                )

                st.dataframe(
                    combo_df,
                    use_container_width=True
                )
                
                
        # =========================
        # MOVE10 × MOMENTUM × BTC 1H
        # =========================

        st.markdown("#### Move10 × Momentum × BTC 1H")

        if (
            "move_10_bars_pct" in setup_diag.columns
            and "signal_momentum" in setup_diag.columns
            and "btc_direction_1h" in setup_diag.columns
        ):

            temp = setup_diag.copy()

            temp["move_10_bars_pct"] = pd.to_numeric(
                temp["move_10_bars_pct"],
                errors="coerce"
            )

            MOVE_BINS = [-999, 0, 2, 5, 10, 20, 999]

            MOVE_LABELS = [
                "<0",
                "0-2",
                "2-5",
                "5-10",
                "10-20",
                ">20"
            ]

            temp["move10_bucket"] = pd.cut(
                temp["move_10_bars_pct"],
                bins=MOVE_BINS,
                labels=MOVE_LABELS,
                include_lowest=True,
            )

            rows = []

            for move_bucket, move_group in temp.groupby(
                "move10_bucket",
                observed=False
            ):

                if len(move_group) == 0:
                    continue

                for momentum, mom_group in move_group.groupby(
                    "signal_momentum",
                    observed=False
                ):

                    if len(mom_group) == 0:
                        continue

                    for btc1h, group in mom_group.groupby(
                        "btc_direction_1h",
                        observed=False
                    ):

                        if len(group) < 2:
                            continue

                        row = swing_stats(
                            f"{move_bucket} | {momentum} | BTC1h {btc1h}",
                            group
                        )

                        if row:
                            row["move10_bucket"] = str(move_bucket)
                            row["momentum"] = momentum
                            row["btc_1h"] = btc1h

                            rows.append(row)

            combo_df = pd.DataFrame(rows)

            if combo_df.empty:
                st.info("No Move10 × Momentum × BTC1H groups.")
            else:

                combo_df = combo_df.sort_values(
                    ["profit_factor", "trades"],
                    ascending=[False, False],
                    na_position="last",
                )

                st.dataframe(
                    combo_df,
                    use_container_width=True
                )
                
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
            
            
@st.cache_data(ttl=60)
def fetch_recent_klines(symbol, interval="15m", limit=25):
    url = "https://fapi.binance.com/fapi/v1/klines"

    try:
        r = requests.get(
            url,
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=5,
        )
        r.raise_for_status()

        df = pd.DataFrame(
            r.json(),
            columns=[
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_volume", "trades",
                "taker_buy_base", "taker_buy_quote", "ignore",
            ],
        )

        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")

        df["time"] = pd.to_datetime(df["open_time"], unit="ms")
        return df

    except Exception:
        return pd.DataFrame()
    
def render_mini_chart(row):
    symbol = row.get("symbol")
    df = fetch_recent_klines(symbol, "15m", 25)

    if df.empty:
        st.warning(f"No chart data for {symbol}")
        return

    fig = go.Figure()

    fig.add_trace(
        go.Candlestick(
            x=df["time"],
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            name=symbol,
        )
    )

    compression_high = row.get("compression_high")
    compression_low = row.get("compression_low")
    breakout_price = row.get("breakout_price")
    entry_price = row.get("entry_price") or row.get("entry_ready_price")

    if compression_high is not None and not pd.isna(compression_high):
        fig.add_hline(y=float(compression_high), line_dash="dash")

    if compression_low is not None and not pd.isna(compression_low):
        fig.add_hline(y=float(compression_low), line_dash="dash")

    if breakout_price is not None and not pd.isna(breakout_price):
        fig.add_hline(y=float(breakout_price), line_dash="dot")

    if entry_price is not None and not pd.isna(entry_price):
        fig.add_hline(y=float(entry_price), line_dash="solid")

    fig.update_layout(
        height=240,
        margin=dict(l=0, r=0, t=10, b=0),
        paper_bgcolor="#0f172a",
        plot_bgcolor="#0f172a",
        font=dict(color="#cbd5e1", size=10),
        xaxis=dict(
            rangeslider=dict(visible=False),
            showgrid=False,
            showticklabels=False,
        ),
        yaxis=dict(
            showgrid=True,
            gridcolor="#1e293b",
            side="right",
        ),
        showlegend=False,
    )

    st.plotly_chart(fig, use_container_width=True)
    
def render_mini_line_chart(row):
    symbol = row.get("symbol")
    df = fetch_recent_klines(symbol, "15m", 40)

    if df.empty:
        st.warning(f"No chart data for {symbol}")
        return

    fig = go.Figure()

    fig.add_trace(
        go.Scatter(
            x=df["time"],
            y=df["close"],
            mode="lines",
            name=symbol,
            line=dict(
                color="#a78bfa",
                width=2,
            )
        )
    )

    compression_high = row.get("compression_high")
    compression_low = row.get("compression_low")
    breakout_price = row.get("breakout_price")
    entry_price = row.get("entry_price") or row.get("entry_ready_price")

    if compression_high is not None and not pd.isna(compression_high):
        fig.add_hline(
            y=float(compression_high),
            line_dash="dash",
            line_color="#38bdf8",
            line_width=1,
        )

    if compression_low is not None and not pd.isna(compression_low):
        fig.add_hline(
            y=float(compression_low),
            line_dash="dash",
            line_color="#38bdf8",
            line_width=1,
        )

    if breakout_price is not None and not pd.isna(breakout_price):
        fig.add_hline(
            y=float(breakout_price),
            line_dash="dot",
            line_color="#22c55e",
            line_width=1,
        )

    if entry_price is not None and not pd.isna(entry_price):
        fig.add_hline(
            y=float(entry_price),
            line_dash="solid",
            line_color="#eab308",
            line_width=1,
        )

    fig.update_layout(
        height=190,
        margin=dict(l=10, r=10, t=8, b=10),
        paper_bgcolor="#0f172a",
        plot_bgcolor="#0f172a",
        font=dict(color="#cbd5e1", size=10),
        xaxis=dict(
            showgrid=True,
            gridcolor="#1e293b",
            showticklabels=False,
            zeroline=False,
        ),
        yaxis=dict(
            showgrid=True,
            gridcolor="#1e293b",
            side="right",
            zeroline=False,
        ),
        showlegend=False,
    )
    
    st.markdown(
    """
    <div style="
        border: 1px solid #263244;
        border-top: 0;
        border-radius: 0 0 14px 14px;
        padding: 0 16px 10px 16px;
        margin-top: -16px;
        margin-bottom: 22px;
        background: linear-gradient(180deg, #111c31 0%, #0f172a 100%);
        box-shadow: 0 8px 24px rgba(0,0,0,0.28);
    ">
    """,
    unsafe_allow_html=True,
    )

    st.plotly_chart(
    fig,
    use_container_width=True,
    config={"displayModeBar": False}
    )
    
    st.markdown(
    "</div>",
    unsafe_allow_html=True,
    )
    
def render_pipeline_card(row):
    state = row.get("state", "N/A")
    symbol = row.get("symbol", "N/A")
    color = state_color(state)
    qcolor = score_color(row.get("compression_score", 0))

    with st.container(border=True):

        # HEADER
        c1, c2 = st.columns([4, 1])

        with c1:
            st.markdown(f"### {symbol}")
            st.markdown(
                f"""
                <span style="
                    background:{color};
                    color:#020617;
                    padding:4px 9px;
                    border-radius:999px;
                    font-size:11px;
                    font-weight:800;
                ">
                    {state}
                </span>
                """,
                unsafe_allow_html=True,
            )

        with c2:
            st.markdown(
                f"""
                <div style="text-align:right;">
                    <div style="font-size:11px; color:#94a3b8;">Pipeline Score</div>
                    <div style="font-size:28px; font-weight:900; color:{color};">
                        {fmt(row.get("pipeline_score"))}
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        st.markdown("---")

        # METRICS
        m1, m2, m3 = st.columns(3)

        with m1:
            st.markdown("##### Compression")
            st.markdown(
                f"""
                <div style="color:{qcolor}; font-size:18px; font-weight:900;">
                    Score {fmt(row.get("compression_score"))}
                </div>
                Range: <b>{fmt(row.get("range_ratio"))}</b><br>
                ATR: <b>{fmt(row.get("atr_ratio"))}</b><br>
                Vol: <b>{fmt(row.get("volume_ratio"))}</b>
                """,
                unsafe_allow_html=True,
            )

        with m2:
            st.markdown("##### Trend / Age")
            st.markdown(
                f"""
                Trend Score: <b>{fmt(row.get("trend_score"))}</b><br>
                Watch Age: <b>{fmt(row.get("watch_age"))}</b><br>
                Waiting: <b>{fmt(row.get("candles_waiting"))}</b><br>
                Reason: <b>{row.get("reason", "N/A")}</b>
                """,
                unsafe_allow_html=True,
            )

        with m3:
            st.markdown("##### Breakout / Pullback")
            st.markdown(
                f"""
                Breakout Price: <b>{fmt(row.get("breakout_price"))}</b><br>
                Pullback: <b>{fmt(row.get("pullback_pct"))}</b><br>
                Hold High: <b>{bool_icon(row.get("holds_compression_high"))}</b><br>
                Continuation: <b>{bool_icon(row.get("continuation"))}</b><br>
                Breakout Detected: <b>{bool_icon(row.get("breakout_detected"))}</b><br>
                Pullback Detected: <b>{bool_icon(row.get("pullback_detected"))}</b>
                """,
                unsafe_allow_html=True,
            )

        render_mini_line_chart(row)
    
with tab_compression_pipeline:
    st.subheader("Compression Pipeline")

    pipeline_df = load_compression_pipeline()

    if pipeline_df.empty:
        st.info("No active compression watches.")

    else:

        st.markdown(
            """
            <style>
            .compression-card-wrap {
                border: 1px solid #263244;
                border-left: 5px solid #a78bfa;
                border-radius: 14px;
                padding: 16px;
                margin-bottom: 22px;
                background:#0f172a;
                box-shadow: 0 8px 24px rgba(0,0,0,0.28);
            }

            .watch-history-card {
                border: 1px solid #263244;
                border-radius: 14px;
                padding: 16px;
                margin-top: 18px;
                margin-bottom: 22px;
                background:#0f172a;
                box-shadow: 0 8px 24px rgba(0,0,0,0.28);
            }

            .watch-history-title {
                font-size: 20px;
                font-weight: 900;
                color: #cbd5e1;
                margin-bottom: 12px;
            }

            .watch-badge {
                display: inline-block;
                padding: 3px 8px;
                border-radius: 999px;
                background: #6d28d9;
                color: white;
                font-size: 11px;
                font-weight: 800;
                margin-left: 8px;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )
    
        # =========================
        # PIPELINE SCORE
        # =========================

        pipeline_df["pipeline_score"] = (
            pipeline_df["compression_score"].fillna(0) * 2
            + pipeline_df["trend_score"].fillna(0)
            + (1 - pipeline_df["range_ratio"].fillna(1)) * 2
            + (1 - pipeline_df["atr_ratio"].fillna(1)) * 2
            + (1 - pipeline_df["volume_ratio"].fillna(1))
        ).round(2)

        # =========================
        # HELPERS VISUALES
        # =========================

        def fmt(value, default="N/A"):
            if value is None or pd.isna(value):
                return default
            if isinstance(value, float):
                return f"{value:.4f}"
            return value

        def bool_icon(value):
            return "✅" if str(value).lower() in ["true", "1", "yes"] else "❌"

        def score_color(score):
            try:
                score = float(score)
            except Exception:
                return "#6b7280"

            if score >= 4:
                return "#22c55e"
            if score >= 3:
                return "#eab308"
            return "#ef4444"

        def state_color(state):
            return {
                "ENTRY_READY": "#22c55e",
                "WAIT_PULLBACK": "#eab308",
                "BREAKOUT_DETECTED": "#38bdf8",
                "WATCHING_COMPRESSION": "#a78bfa",
                "EXPIRED": "#ef4444",
            }.get(state, "#6b7280")

        def card_html(row):
            state = row.get("state", "N/A")
            color = state_color(state)
            qcolor = score_color(row.get("compression_score", 0))

            return f"""
            <div
            style="
                    border: 1px solid #263244;
                    border-left: 5px solid {color};
                    border-radius: 14px 14px 0 0;
                    border-bottom: 0;
                    padding: 16px 16px 10px 16px;
                    margin-bottom: 0px;
                    background:#0f172a;
                    box-shadow: 0 8px 24px rgba(0,0,0,0.28);
                "
            >
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <div>
                        <div style="font-size:20px; font-weight:800; color:#ffffff;">
                            {row.get("symbol", "N/A")}
                        </div>
                        <div style="
                            display:inline-block;
                            margin-top:6px;
                            padding:4px 9px;
                            border-radius:999px;
                            background:{color};
                            color:#020617;
                            font-size:12px;
                            font-weight:800;
                        ">
                            {state}
                        </div>
                    </div>

                    <div style="text-align:right;">
                        <div style="font-size:12px; color:#94a3b8;">Pipeline Score</div>
                        <div style="font-size:28px; font-weight:900; color:{color};">
                            {fmt(row.get("pipeline_score"))}
                        </div>
                    </div>
                </div>

                <hr style="border-color:#1e293b; margin:14px 0;" />

                <div style="display:grid; grid-template-columns: repeat(3, 1fr); gap:14px;">
                    <div>
                        <div style="color:#94a3b8; font-size:12px;">Compression</div>
                        <div style="font-size:18px; font-weight:800; color:{qcolor};">
                            Score {fmt(row.get("compression_score"))}
                        </div>
                        <div style="color:#cbd5e1;">Range: <b>{fmt(row.get("range_ratio"))}</b></div>
                        <div style="color:#cbd5e1;">ATR: <b>{fmt(row.get("atr_ratio"))}</b></div>
                        <div style="color:#cbd5e1;">Vol: <b>{fmt(row.get("volume_ratio"))}</b></div>
                    </div>

                    <div>
                        <div style="color:#94a3b8; font-size:12px;">Trend / Age</div>
                        <div style="color:#cbd5e1;">Trend Score: <b>{fmt(row.get("trend_score"))}</b></div>
                        <div style="color:#cbd5e1;">Watch Age: <b>{fmt(row.get("watch_age"))}</b></div>
                        <div style="color:#cbd5e1;">Waiting: <b>{fmt(row.get("candles_waiting"))}</b></div>
                        <div style="color:#cbd5e1;">Reason: <b>{row.get("reason", "N/A")}</b></div>
                    </div>

                    <div>
                        <div style="color:#94a3b8; font-size:12px;">Breakout / Pullback</div>
                        <div style="color:#cbd5e1;">Breakout Price: <b>{fmt(row.get("breakout_price"))}</b></div>
                        <div style="color:#cbd5e1;">Pullback: <b>{fmt(row.get("pullback_pct"))}</b></div>
                        <div style="color:#cbd5e1;">Hold High: <b>{bool_icon(row.get("holds_compression_high"))}</b></div>
                        <div style="color:#cbd5e1;">Continuation: <b>{bool_icon(row.get("continuation"))}</b></div>
                        <div style="color:#cbd5e1;">Breakout Detected: <b>{bool_icon(row.get("breakout_detected"))}</b></div>
                        <div style="color:#cbd5e1;">Breakout Confirmed: <b>{bool_icon(row.get("breakout_confirmed"))}</b></div>
                        <div style="color:#cbd5e1;">Pullback Detected: <b>{bool_icon(row.get("pullback_detected"))}</b></div>
                        <div style="color:#cbd5e1;">Continuation Detected: <b>{bool_icon(row.get("continuation_detected"))}</b></div>
                    </div>
                </div>
            </div>
            """

        # =========================
        # SUMMARY METRICS
        # =========================

        state_counts = pipeline_df["state"].value_counts()

        c1, c2, c3, c4, c5 = st.columns(5)

        c1.metric("ENTRY_READY", int(state_counts.get("ENTRY_READY", 0)))
        c2.metric("WAIT_PULLBACK", int(state_counts.get("WAIT_PULLBACK", 0)))
        c3.metric("BREAKOUT", int(state_counts.get("BREAKOUT_DETECTED", 0)))
        c4.metric("WATCHING", int(state_counts.get("WATCHING_COMPRESSION", 0)))
        c5.metric("EXPIRED", int(state_counts.get("EXPIRED", 0)))

        st.markdown("---")

        # =========================
        # FILTERS
        # =========================

        symbols = sorted(pipeline_df["symbol"].dropna().unique())

        selected_symbol = st.selectbox(
            "Inspect symbol",
            options=["ALL"] + symbols,
            index=0,
            key="pipeline_symbol_filter_cards",
        )

        min_score = st.slider(
            "Min Pipeline Score",
            min_value=0.0,
            max_value=float(max(20, pipeline_df["pipeline_score"].max())),
            value=0.0,
            step=0.5,
            key="pipeline_min_score_cards",
        )

        view_df = pipeline_df.copy()

        if selected_symbol != "ALL":
            view_df = view_df[
                view_df["symbol"] == selected_symbol
            ].copy()
            

        view_df = view_df[
            view_df["pipeline_score"] >= min_score
        ].copy()

        view_df = view_df.sort_values(
            ["pipeline_score", "compression_score", "trend_score", "watch_age"],
            ascending=[False, False, False, False],
        )

        # =========================
        # CARDS BY STATE
        # =========================

        states_order = [
            "ENTRY_READY",
            "WAIT_PULLBACK",
            "BREAKOUT_DETECTED",
            "WATCHING_COMPRESSION",
            "EXPIRED",
        ]

        for state in states_order:
            state_df = view_df[view_df["state"] == state]

            if state_df.empty:
                continue

            st.markdown(f"### {state} ({len(state_df)})")

            for _, row in state_df.iterrows():
                render_pipeline_card(row)

        # ============================================
        # WATCH HISTORY (SOLO SI HAY UN SÍMBOLO)
        # ============================================

        if selected_symbol != "ALL":

            history_df = load_watch_history(
                selected_symbol,
                limit=30,
            )

            st.markdown(
                f"""
                <div class="watch-history-card">
                    <div class="watch-history-title">
                        WATCH HISTORY ({selected_symbol})
                        <span class="watch-badge">
                            {len(history_df)} events
                        </span>
                    </div>
                """,
                unsafe_allow_html=True,
            )

            if history_df.empty:
                st.info(
                    f"No watch history yet for {selected_symbol}."
                )

            else:

                history_cols = [
                    "logged_at",
                    "event",
                    "reason",
                    "watch_age",
                    "candles_waiting",
                    "compression_score",
                    "trend_score",
                    "compression_high",
                    "compression_low",
                    "range_ratio",
                    "atr_ratio",
                    "volume_ratio",
                    "avg_body_pct",
                    "breakout_detected",
                    "breakout_reason",
                    "breakout_volume_ratio",
                    "breakout_price",
                    "breakout_extension_pct",
                    "breakout_extension_atr",
                    "pullback_pct",
                    "valid_pullback",
                    "holds_compression_high",
                    "continuation",
                ]

                existing_history_cols = [
                    c for c in history_cols
                    if c in history_df.columns
                ]

                st.dataframe(
                    history_df[existing_history_cols],
                    use_container_width=True,
                    hide_index=True,
                )

                with st.expander(
                    "Last 10 candles from latest journal event"
                ):

                    latest = history_df.iloc[0]

                    candles = latest.get("last_10_candles")

                    if isinstance(candles, list):

                        st.dataframe(
                            pd.DataFrame(candles),
                            use_container_width=True,
                            hide_index=True,
                        )

                    else:
                        st.info("No last_10_candles available.")

            st.markdown(
                "</div>",
                unsafe_allow_html=True,
            )

        # =========================
        # OPTIONAL RAW TABLE
        # =========================

        with st.expander("Raw pipeline table"):
            cols = [
                "symbol",
                "state",
                "pipeline_score",
                "reason",
                "watch_age",
                "candles_waiting",
                "compression_score",
                "trend_score",
                "range_ratio",
                "atr_ratio",
                "volume_ratio",
                "breakout_detected",
                "breakout_confirmed",
                "compression_high",
                "compression_low",
                "breakout_price",
                "breakout_volume_ratio",
                "breakout_extension_pct",
                "breakout_extension_atr",
                "pullback_pct",
                "pullback_detected",
                "valid_pullback",
                "holds_compression_high",
                "continuation",
                "continuation_detected",
                "entry_ready",
            ]

            existing_cols = [c for c in cols if c in view_df.columns]

            st.dataframe(
                view_df[existing_cols],
                use_container_width=True,
                hide_index=True,
            )