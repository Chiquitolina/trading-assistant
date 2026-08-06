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

import numpy as np

# =========================
# CONFIG
# =========================
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR))

load_dotenv(BASE_DIR / ".env")

from engine.backtest.metrics import calculate_metrics  # noqa
from dashboard.analytics.mfe_mae import build_mfe_mae_report

from dashboard.services.trade_inspector_service import (
    TradeInspectorService,
)

from dashboard.charts.trade_inspector_chart import (
    build_trade_inspector_chart,
)

TRADES_FILE = BASE_DIR / "trades.csv"
PAPER_SIGNALS_FILE = BASE_DIR / "paper_signals.csv"
STATUS_FILE = BASE_DIR / "status.json"

POST_TRADE_REPLAY_FILE = (
    BASE_DIR
    / "reports"
    / "post_trade_replay.csv"
)

TP_SL_SCENARIOS_FILE = (
    BASE_DIR
    / "reports"
    / "tp_sl_scenarios.csv"
)

PARTIAL_TP_SCENARIOS_FILE = (
    BASE_DIR
    / "reports"
    / "partial_tp_scenarios.csv"
)
TZ = "America/Argentina/Buenos_Aires"
SYMBOL = "BTCUSDT"
STATUS_TTL_SECONDS = 10
CANDLE_MINUTES = 15

trade_inspector_service = TradeInspectorService()

st.set_page_config(
    page_title="Trade Journal",
    layout="wide"
)

st.title("📊 Trade Journal Dashboard")
REFRESH_SECONDS = st.sidebar.slider(
    "Auto refresh seconds",
    min_value=2,
    max_value=60,
    value=10,
    step=1,
)

st_autorefresh(
    interval=REFRESH_SECONDS * 1000,
    key="dashboard_refresh",
)

@st.cache_data(ttl=10)
def load_csv_cached(path):
    path = Path(path)

    if not path.exists():
        return pd.DataFrame()

    return pd.read_csv(path)


# =========================
# HELPERS
# =========================

def timeframe_to_minutes(timeframe):
    mapping = {
        "1m": 1,
        "3m": 3,
        "5m": 5,
        "15m": 15,
        "30m": 30,
        "1h": 60,
        "2h": 120,
        "4h": 240,
    }

    return mapping.get(timeframe, 30)

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

def render_trade_inspector_for_row(
    row,
    status="CLOSED",
    key_prefix="trade_inspector",
):
    inspection = trade_inspector_service.inspect(
        row=row,
        status=status,
    )

    selected_trade = inspection.trade

    inspector_timeframe = str(
        row.get("trigger_tf", "30m") or "30m"
    )

    valid_timeframes = {
        "1m",
        "3m",
        "5m",
        "15m",
        "30m",
        "1h",
        "2h",
        "4h",
    }

    if inspector_timeframe not in valid_timeframes:
        inspector_timeframe = "30m"

    interval_minutes = timeframe_to_minutes(
        inspector_timeframe
    )

    with st.spinner(
        f"Loading {inspector_timeframe} candles "
        f"for {selected_trade.symbol}..."
    ):
        inspection.candles = (
            trade_inspector_service.load_trade_candles(
                trade=selected_trade,
                interval=inspector_timeframe,
                candles_before=20,
                candles_after=12,
            )
        )

    st.markdown("---")

    st.subheader(
        f"🔎 Trade Inspector — {selected_trade.symbol}"
    )

    st.caption(
        f"Inspection timeframe: {inspector_timeframe}"
    )

    c1, c2, c3, c4 = st.columns(4)

    c1.metric(
        "Status",
        selected_trade.status or "-",
    )

    c2.metric(
        "Side",
        selected_trade.side or "-",
    )

    c3.metric(
        "Entry",
        fmt_price_for_display(
            selected_trade.entry_price
        ),
    )

    c4.metric(
        "Exit",
        fmt_price_for_display(
            selected_trade.exit_price
        ),
    )

    c5, c6, c7, c8 = st.columns(4)

    c5.metric(
        "Compression High",
        fmt_price_for_display(
            selected_trade.compression_high
        ),
    )

    c6.metric(
        "Compression Low",
        fmt_price_for_display(
            selected_trade.compression_low
        ),
    )

    c7.metric(
        "Breakout",
        fmt_price_for_display(
            selected_trade.breakout_price
        ),
    )

    c8.metric(
        "Entry Ready",
        fmt_price_for_display(
            selected_trade.entry_ready_price
        ),
    )

    candles_df = inspection.candles

    if candles_df.empty:
        st.warning(
            f"No se pudieron cargar las velas "
            f"{inspector_timeframe} para este trade."
        )
        return

    inspector_fig = build_trade_inspector_chart(
        inspection=inspection,
        interval_minutes=interval_minutes,
        timeframe=inspector_timeframe,
    )

    st.plotly_chart(
        inspector_fig,
        use_container_width=True,
        key=f"{key_prefix}_chart",
        config={
            "displaylogo": False,
            "scrollZoom": True,
        },
    )

    with st.expander(
        f"{inspector_timeframe} candles",
        expanded=False,
    ):
        candle_columns = [
            "open_ts",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]

        available_candle_columns = [
            col
            for col in candle_columns
            if col in candles_df.columns
        ]

        st.dataframe(
            candles_df[available_candle_columns],
            use_container_width=True,
            hide_index=True,
        )

def render_bucket_robustness_explorer(
    source_df,
    summary_df,
    first_bucket_col,
    second_bucket_col,
    first_bucket_label,
    second_bucket_label,
    key_prefix,
):
    st.markdown("#### 🔎 Analyze selected buckets")

    if source_df.empty or summary_df.empty:
        st.info("No bucket combinations available.")
        return

    required = [
        "symbol",
        "pnl",
        first_bucket_col,
        second_bucket_col,
    ]

    missing = [
        col for col in required
        if col not in source_df.columns
    ]

    if missing:
        st.info(f"Missing explorer columns: {missing}")
        return

    options_df = summary_df.copy()

    options_df = options_df[
        options_df["trades"] > 0
    ].copy()

    options_df["first_bucket_value"] = (
        options_df[first_bucket_col].astype(str)
    )

    options_df["second_bucket_value"] = (
        options_df[second_bucket_col].astype(str)
    )

    options_df["bucket_key"] = (
        options_df["first_bucket_value"]
        + " || "
        + options_df["second_bucket_value"]
    )

    options_df["bucket_label"] = (
        options_df["first_bucket_value"]
        + " × "
        + options_df["second_bucket_value"]
        + " — "
        + options_df["trades"].astype(int).astype(str)
        + " trades"
        + " | WR "
        + options_df["winrate"].round(2).astype(str)
        + "%"
        + " | PF "
        + options_df["pf"].fillna(0).round(2).astype(str)
    )

    options_df = options_df.sort_values(
        ["pf", "total_pnl"],
        ascending=False,
        na_position="last",
    )

    label_to_key = dict(zip(
        options_df["bucket_label"],
        options_df["bucket_key"],
    ))

    selected_labels = st.multiselect(
        "Select one or more bucket combinations",
        options=options_df["bucket_label"].tolist(),
        key=f"{key_prefix}_selected_buckets",
    )

    if not selected_labels:
        st.caption(
            "Select one or more rows to inspect their trades together."
        )
        return

    selected_keys = {
        label_to_key[label]
        for label in selected_labels
    }

    work = source_df.copy()

    work["_first_bucket"] = (
        work[first_bucket_col].astype(str)
    )

    work["_second_bucket"] = (
        work[second_bucket_col].astype(str)
    )

    work["_bucket_key"] = (
        work["_first_bucket"]
        + " || "
        + work["_second_bucket"]
    )

    selected_rows = work[
        work["_bucket_key"].isin(selected_keys)
    ].copy()

    selected_rows["pnl"] = pd.to_numeric(
        selected_rows["pnl"],
        errors="coerce",
    )

    selected_rows = selected_rows.dropna(subset=["pnl"])

    # ==========================================
    # TRADE KEY
    # ==========================================
    if "trade_id" in selected_rows.columns:
        selected_rows["trade_key"] = (
            selected_rows["trade_id"].astype(str)
        )

    elif "entry_ts" in selected_rows.columns:
        selected_rows["trade_key"] = (
            selected_rows["symbol"].astype(str)
            + "_"
            + selected_rows["entry_ts"].astype(str)
        )

    elif "entry_ts_dt" in selected_rows.columns:
        selected_rows["trade_key"] = (
            selected_rows["symbol"].astype(str)
            + "_"
            + selected_rows["entry_ts_dt"].astype(str)
        )

    else:
        selected_rows["trade_key"] = (
            selected_rows["symbol"].astype(str)
            + "_row_"
            + selected_rows.index.astype(str)
        )

    total_rows = len(selected_rows)
    unique_trades = selected_rows["trade_key"].nunique()
    duplicate_rows = selected_rows["trade_key"].duplicated().sum()

    # Los reportes estadísticos utilizan trades únicos.
    edge_df = selected_rows.drop_duplicates(
        subset=["trade_key"],
        keep="first",
    ).copy()

    edge_df["is_win"] = edge_df["pnl"] > 0

    # ==========================================
    # TIMESTAMP
    # ==========================================
    if "entry_ts_dt" in edge_df.columns:
        edge_df["entry_datetime"] = pd.to_datetime(
            edge_df["entry_ts_dt"],
            errors="coerce",
            utc=True,
        )

    elif "entry_ts" in edge_df.columns:
        numeric_entry_ts = pd.to_numeric(
            edge_df["entry_ts"],
            errors="coerce",
        )

        edge_df["entry_datetime"] = pd.to_datetime(
            numeric_entry_ts,
            unit="ms",
            errors="coerce",
            utc=True,
        )

        missing_datetime = edge_df["entry_datetime"].isna()

        if missing_datetime.any():
            edge_df.loc[
                missing_datetime,
                "entry_datetime",
            ] = pd.to_datetime(
                edge_df.loc[missing_datetime, "entry_ts"],
                errors="coerce",
                utc=True,
            )

    else:
        edge_df["entry_datetime"] = pd.NaT

    edge_df["entry_date"] = (
        edge_df["entry_datetime"].dt.date
    )

    edge_df["entry_30m"] = (
        edge_df["entry_datetime"].dt.floor("30min")
    )

    # ==========================================
    # SUMMARY HELPERS
    # ==========================================
    def edge_summary(data):
        if data.empty:
            return {
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "winrate": 0.0,
                "total_pnl": 0.0,
                "avg_pnl": 0.0,
                "pf": None,
                "avg_mfe": None,
                "avg_mae": None,
            }

        pnl = pd.to_numeric(
            data["pnl"],
            errors="coerce",
        ).dropna()

        wins = int((pnl > 0).sum())
        losses = int((pnl <= 0).sum())
        
        avg_mfe = (
            pd.to_numeric(
                data["max_favorable_pct"],
                errors="coerce",
            ).mean()
            if "max_favorable_pct" in data.columns
            else None
        )

        avg_mae = (
            pd.to_numeric(
                data["max_adverse_pct"],
                errors="coerce",
            ).mean()
            if "max_adverse_pct" in data.columns
            else None
        )

        return {
            "trades": len(pnl),
            "wins": wins,
            "losses": losses,
            "winrate": (
                wins / len(pnl) * 100
                if len(pnl) > 0
                else 0
            ),
            "total_pnl": pnl.sum(),
            "avg_pnl": pnl.mean(),
            "pf": profit_factor(pnl),
            "avg_mfe": avg_mfe,
            "avg_mae": avg_mae,
        }

    def render_summary_metrics(data):
        result = edge_summary(data)

        c1, c2, c3, c4 = st.columns(4)

        c1.metric("Trades", result["trades"])
        c2.metric("Winrate", f'{result["winrate"]:.2f}%')
        c3.metric("Total PnL", f'{result["total_pnl"]:.4f}')
        c4.metric(
            "Profit Factor",
            (
                f'{result["pf"]:.2f}'
                if result["pf"] is not None
                else "∞"
            ),
        )
        
        c5, c6 = st.columns(2)

        c5.metric(
            "Avg MFE",
            (
                f'{result["avg_mfe"]:.4f}%'
                if pd.notna(result["avg_mfe"])
                else "N/A"
            ),
        )

        c6.metric(
            "Avg MAE",
            (
                f'{result["avg_mae"]:.4f}%'
                if pd.notna(result["avg_mae"])
                else "N/A"
            ),
        )

    def build_group_report(data, group_col):
        valid = data.dropna(subset=[group_col]).copy()

        if valid.empty:
            return pd.DataFrame()

        report = (
            valid
            .groupby(group_col, dropna=False)
            .agg(
                trades=("trade_key", "nunique"),
                wins=("pnl", lambda x: int((x > 0).sum())),
                losses=("pnl", lambda x: int((x <= 0).sum())),
                winrate=(
                    "pnl",
                    lambda x: round((x > 0).mean() * 100, 2),
                ),
                avg_pnl=("pnl", "mean"),
                total_pnl=("pnl", "sum"),
                avg_mfe=("max_favorable_pct", "mean"),
                avg_mae=("max_adverse_pct", "mean"),
                pf=("pnl", profit_factor),
            )
            .reset_index()
        )

        report["trade_share_pct"] = (
            report["trades"]
            / edge_df["trade_key"].nunique()
            * 100
        ).round(2)

        for col in [
            "avg_pnl",
            "total_pnl",
            "avg_mfe",
            "avg_mae",
        ]:
            report[col] = report[col].round(4)

        return report.sort_values(
            ["trades", "total_pnl"],
            ascending=False,
        )

    # ==========================================
    # MAIN METRICS
    # ==========================================
    st.markdown(
        f"##### Selected edge: "
        f"{first_bucket_label} × {second_bucket_label}"
    )

    u1, u2, u3 = st.columns(3)

    u1.metric("Selected rows", total_rows)
    u2.metric("Unique trades", unique_trades)
    u3.metric("Duplicate rows", int(duplicate_rows))

    render_summary_metrics(edge_df)

    if duplicate_rows > 0:
        st.warning(
            "Duplicate rows were excluded from all statistical reports."
        )
    else:
        st.success(
            "All selected rows correspond to unique trades."
        )

    (
        trades_tab,
        days_tab,
        symbols_tab,
        batches_tab,
        btc_tab,
        robustness_tab,
    ) = st.tabs([
        "All Trades",
        "By Day",
        "By Symbol",
        "30m Batches",
        "BTC Context",
        "Robustness",
    ])

    # ==========================================
    # ALL TRADES
    # ==========================================
    with trades_tab:
        preferred_cols = [
            # Identidad
            "trade_key",
            "symbol",
            "side",

            # Timestamps
            "compression_created_ts_dt",
            "signal_ts_dt",
            "breakout_ts_dt",
            "entry_ready_ts_dt",
            "entry_ts_dt",
            "exit_ts_dt",

            # Resultado
            "pnl",
            "exit_reason",
            "leverage",
            "pnl_unleveraged_pct",
            "realized_r",

            # Buckets seleccionados
            first_bucket_col,
            second_bucket_col,

            # Precios
            "signal_price",
            "compression_high",
            "compression_low",
            "breakout_price",
            "entry_ready_price",
            "entry",
            "real_entry",
            "tp",
            "sl",

            # Distancias y riesgo
            "entry_vs_compression_pct",
            "real_entry_vs_compression_pct",
            "entry_vs_breakout_pct",
            "real_entry_vs_breakout_pct",
            "real_entry_vs_ready_pct",
            "signal_to_real_entry_pct",

            "structural_risk_pct",
            "structural_risk_bucket",
            "planned_reward_pct",
            "planned_rr",

            # Breakout
            "breakout_extension_atr",
            "breakout_extension_pct",
            "breakout_volume_ratio",

            # Duraciones
            "watch_to_breakout_minutes",
            "breakout_to_ready_minutes",
            "ready_to_entry_seconds",

            # Compresión
            "compression_shape",
            "compression_quality_label",
            "compression_score",
            "trend_score",
            "range_ratio",
            "atr_ratio",
            "volume_ratio",

            # MFE / MAE
            "max_favorable_pct",
            "max_adverse_pct",

            # BTC
            "btc_corr_5m_1h",
            "btc_beta_5m_1h",
            "btc_r2_5m_1h",

            # Configuración
            "trigger_tf",
            "strategy_mode",
            "base_mode",
            "selected_lookback",
            "compression_base_lookback",
        ]

        available_cols = [
            col for col in preferred_cols
            if col in edge_df.columns
        ]

        trade_detail_df = edge_df.sort_values(
            "entry_datetime"
            if edge_df["entry_datetime"].notna().any()
            else "pnl",
            ascending=False,
        )

        inspector_source_df = (
            trade_detail_df
            .reset_index(drop=True)
        )

        trade_display_df = inspector_source_df[
            available_cols
        ].copy()

        st.caption(
            "Seleccioná un trade para inspeccionar "
            "gráficamente su compresión."
        )

        bucket_trades_event = st.dataframe(
            trade_display_df,
            use_container_width=True,
            hide_index=True,
            key=f"{key_prefix}_trade_selector",
            on_select="rerun",
            selection_mode="single-row",
        )

        selected_trade_rows = (
            bucket_trades_event.selection.rows
        )

        if selected_trade_rows:
            selected_position = selected_trade_rows[0]

            selected_trade_row = (
                inspector_source_df.iloc[
                    selected_position
                ]
            )

            render_trade_inspector_for_row(
                row=selected_trade_row,
                status="CLOSED",
                key_prefix=(
                    f"{key_prefix}_"
                    f"{selected_trade_row['trade_key']}"
                ),
            )

    # ==========================================
    # BY DAY
    # ==========================================
    with days_tab:
        day_report = build_group_report(
            edge_df,
            "entry_date",
        )

        if day_report.empty:
            st.info("No valid entry dates available.")

        else:
            d1, d2 = st.columns(2)

            d1.metric(
                "Unique Days",
                day_report["entry_date"].nunique(),
            )
            d2.metric(
                "Largest Day Share",
                f'{day_report["trade_share_pct"].max():.2f}%',
            )

            st.dataframe(
                day_report,
                use_container_width=True,
                hide_index=True,
            )

    # ==========================================
    # BY SYMBOL
    # ==========================================
    with symbols_tab:
        symbol_report = build_group_report(
            edge_df,
            "symbol",
        )

        if symbol_report.empty:
            st.info("No symbols available.")

        else:
            s1, s2 = st.columns(2)

            s1.metric(
                "Unique Symbols",
                edge_df["symbol"].nunique(),
            )
            s2.metric(
                "Largest Symbol Share",
                f'{symbol_report["trade_share_pct"].max():.2f}%',
            )

            st.dataframe(
                symbol_report,
                use_container_width=True,
                hide_index=True,
            )

    # ==========================================
    # 30M BATCHES
    # ==========================================
    with batches_tab:
        valid_batches = edge_df.dropna(
            subset=["entry_30m"]
        )

        if valid_batches.empty:
            st.info("No valid entry timestamps available.")

        else:
            batch_report = (
                valid_batches
                .groupby("entry_30m")
                .agg(
                    trades=("trade_key", "nunique"),
                    symbols=(
                        "symbol",
                        lambda x: ", ".join(
                            sorted(set(x.astype(str)))
                        ),
                    ),
                    wins=("pnl", lambda x: int((x > 0).sum())),
                    losses=("pnl", lambda x: int((x <= 0).sum())),
                    total_pnl=("pnl", "sum"),
                    pf=("pnl", profit_factor),
                )
                .reset_index()
                .sort_values(
                    ["trades", "total_pnl"],
                    ascending=False,
                )
            )

            batch_report["trade_share_pct"] = (
                batch_report["trades"]
                / unique_trades
                * 100
            ).round(2)

            batch_report["total_pnl"] = (
                batch_report["total_pnl"].round(4)
            )

            b1, b2 = st.columns(2)

            b1.metric(
                "Unique 30m Batches",
                len(batch_report),
            )
            b2.metric(
                "Largest Batch",
                int(batch_report["trades"].max()),
            )

            st.dataframe(
                batch_report,
                use_container_width=True,
                hide_index=True,
            )

    # ==========================================
    # BTC CONTEXT
    # ==========================================
    with btc_tab:
        btc_context_cols = []

        for col in edge_df.columns:
            if "btc" not in col.lower():
                continue

            unique_values = edge_df[col].nunique(
                dropna=True
            )

            if 1 < unique_values <= 30:
                btc_context_cols.append(col)

        if not btc_context_cols:
            st.info(
                "No categorical BTC context columns available."
            )

        else:
            selected_btc_cols = st.multiselect(
                "BTC context dimensions",
                options=btc_context_cols,
                default=btc_context_cols[:3],
                key=f"{key_prefix}_btc_dimensions",
            )

            if not selected_btc_cols:
                st.caption(
                    "Select at least one BTC context dimension."
                )

            for btc_col in selected_btc_cols:
                st.markdown(f"##### {btc_col}")

                btc_report = build_group_report(
                    edge_df,
                    btc_col,
                )

                st.dataframe(
                    btc_report,
                    use_container_width=True,
                    hide_index=True,
                )

    # ==========================================
    # ROBUSTNESS TEST
    # ==========================================
    with robustness_tab:
        robustness_rows = []

        baseline = edge_summary(edge_df)

        robustness_rows.append({
            "scenario": "Original selected buckets",
            **baseline,
        })

        # Remove the day producing the most PnL.
        valid_days = edge_df.dropna(
            subset=["entry_date"]
        )

        if not valid_days.empty:
            pnl_by_day = valid_days.groupby(
                "entry_date"
            )["pnl"].sum()

            best_day = pnl_by_day.idxmax()

            without_best_day = edge_df[
                edge_df["entry_date"] != best_day
            ]

            robustness_rows.append({
                "scenario": f"Without best day: {best_day}",
                **edge_summary(without_best_day),
            })

        # Remove the symbol producing the most PnL.
        if "symbol" in edge_df.columns:
            pnl_by_symbol = edge_df.groupby(
                "symbol"
            )["pnl"].sum()

            if not pnl_by_symbol.empty:
                best_symbol = pnl_by_symbol.idxmax()

                without_best_symbol = edge_df[
                    edge_df["symbol"] != best_symbol
                ]

                robustness_rows.append({
                    "scenario": (
                        f"Without best symbol: {best_symbol}"
                    ),
                    **edge_summary(without_best_symbol),
                })

        # Remove batch containing the most trades.
        valid_batches = edge_df.dropna(
            subset=["entry_30m"]
        )

        if not valid_batches.empty:
            batch_sizes = valid_batches.groupby(
                "entry_30m"
            )["trade_key"].nunique()

            largest_batch = batch_sizes.idxmax()

            without_largest_batch = edge_df[
                edge_df["entry_30m"] != largest_batch
            ]

            robustness_rows.append({
                "scenario": (
                    f"Without largest batch: {largest_batch}"
                ),
                **edge_summary(without_largest_batch),
            })

        robustness_df = pd.DataFrame(
            robustness_rows
        )

        for col in [
            "winrate",
            "total_pnl",
            "avg_pnl",
            "pf",
        ]:
            if col in robustness_df.columns:
                robustness_df[col] = pd.to_numeric(
                    robustness_df[col],
                    errors="coerce",
                ).round(4)

        st.dataframe(
            robustness_df,
            use_container_width=True,
            hide_index=True,
        )

        if len(robustness_df) > 1:
            positive_after_tests = (
                robustness_df.iloc[1:]["total_pnl"] > 0
            ).all()

            pf_after_tests = (
                robustness_df.iloc[1:]["pf"]
                .fillna(999)
                > 1
            ).all()

            if positive_after_tests and pf_after_tests:
                st.success(
                    "The selected edge remains positive after all "
                    "concentration tests."
                )
            else:
                st.warning(
                    "The selected edge weakens after removing one "
                    "of its main concentration sources."
                )

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

def replay_profit_factor(values):
    values = pd.to_numeric(
        values,
        errors="coerce",
    ).dropna()

    gross_profit = values[values > 0].sum()
    gross_loss = abs(values[values < 0].sum())

    if gross_loss == 0:
        return None

    return gross_profit / gross_loss


def replay_max_drawdown(
    data,
    pnl_col,
    time_col="entry_ts",
):
    if (
        data.empty
        or pnl_col not in data.columns
        or time_col not in data.columns
    ):
        return 0.0

    work = data.copy()

    work[pnl_col] = pd.to_numeric(
        work[pnl_col],
        errors="coerce",
    )

    work[time_col] = pd.to_datetime(
        work[time_col],
        utc=True,
        errors="coerce",
    )

    work = (
        work
        .dropna(subset=[pnl_col, time_col])
        .sort_values(time_col)
    )

    if work.empty:
        return 0.0

    equity = work[pnl_col].cumsum()

    equity = pd.concat(
        [
            pd.Series([0.0]),
            equity.reset_index(drop=True),
        ],
        ignore_index=True,
    )

    equity_peak = equity.cummax()
    drawdown = equity - equity_peak

    return float(drawdown.min())


def normalize_replay_bool(series):
    if pd.api.types.is_bool_dtype(series):
        return series.fillna(False)

    return (
        series
        .astype(str)
        .str.strip()
        .str.lower()
        .isin(["true", "1", "yes"])
    )

def compression_profit_factor(series):
    pnl = pd.to_numeric(series, errors="coerce").dropna()

    gross_profit = pnl[pnl > 0].sum()
    gross_loss = abs(pnl[pnl < 0].sum())

    if gross_loss == 0:
        return None

    return round(gross_profit / gross_loss, 2)


def build_compression_analytics_report(
    data: pd.DataFrame,
    group_cols: list[str],
    min_trades: int = 1,
) -> pd.DataFrame:
    """
    Builds a performance report for one or more compression dimensions.

    Examples:
        ["compression_shape"]
        ["compression_quality_label"]
        ["side", "compression_shape"]
        ["compression_shape", "compression_duration_bucket"]
    """

    required_cols = group_cols + ["pnl"]

    missing = [col for col in required_cols if col not in data.columns]

    if missing:
        return pd.DataFrame()

    work = data.copy()

    work["pnl"] = pd.to_numeric(work["pnl"], errors="coerce")

    for col in ["max_favorable_pct", "max_adverse_pct", "pnl_usd"]:
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")

    work = work.dropna(subset=group_cols + ["pnl"])

    if work.empty:
        return pd.DataFrame()

    aggregations = {
        "trades": ("pnl", "count"),
        "wins": ("pnl", lambda x: int((x > 0).sum())),
        "losses": ("pnl", lambda x: int((x <= 0).sum())),
        "winrate": (
            "pnl",
            lambda x: round((x > 0).mean() * 100, 2),
        ),
        "avg_pnl": ("pnl", "mean"),
        "total_pnl": ("pnl", "sum"),
        "median_pnl": ("pnl", "median"),
        "avg_win": (
            "pnl",
            lambda x: x[x > 0].mean() if (x > 0).any() else 0,
        ),
        "avg_loss": (
            "pnl",
            lambda x: x[x <= 0].mean() if (x <= 0).any() else 0,
        ),
        "profit_factor": ("pnl", compression_profit_factor),
    }

    if "max_favorable_pct" in work.columns:
        aggregations["avg_mfe"] = ("max_favorable_pct", "mean")

    if "max_adverse_pct" in work.columns:
        aggregations["avg_mae"] = ("max_adverse_pct", "mean")

    if "pnl_usd" in work.columns:
        aggregations["total_pnl_usd"] = ("pnl_usd", "sum")

    report = (
        work
        .groupby(group_cols, observed=False)
        .agg(**aggregations)
        .reset_index()
    )

    report = report[report["trades"] >= min_trades].copy()

    numeric_round_cols = [
        "avg_pnl",
        "total_pnl",
        "median_pnl",
        "avg_win",
        "avg_loss",
        "avg_mfe",
        "avg_mae",
        "total_pnl_usd",
    ]

    for col in numeric_round_cols:
        if col in report.columns:
            report[col] = report[col].round(4)

    if report.empty:
        return report

    report = report.sort_values(
        by=["profit_factor", "total_pnl", "winrate", "trades"],
        ascending=[False, False, False, False],
        na_position="last",
    )

    return report


def add_compression_analytics_buckets(data: pd.DataFrame) -> pd.DataFrame:
    """
    Creates categorical buckets without modifying the original dataframe.
    """

    out = data.copy()

    numeric_cols = [
        # Detector
        "compression_score",
        "range_ratio",
        "atr_ratio",
        "volume_ratio",
        "avg_body_pct",
        "compression_range_pct",

        # Structure
        "compression_height_pct",
        "compression_duration",
        "upper_slope",
        "lower_slope",
        "slope_difference",
        "touches_high",
        "touches_low",
        "inside_ratio",
        
        # Breakout
        "breakout_volume_ratio",
        "breakout_extension_pct",
        "breakout_extension_atr",

        # Entry
        "entry_distance_pct",
        "entry_vs_compression_pct",
        "entry_vs_breakout_pct",
    ]

    for col in numeric_cols:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
            
    # =========================
    # DETECTOR COMPONENTS
    # =========================

    # Compression score: cantidad de condiciones cumplidas
    if "compression_score" in out.columns:
        out["compression_score_bucket"] = pd.cut(
            out["compression_score"],
            bins=[-np.inf, 0.5, 1.5, 2.5, 3.5, 4.5, np.inf],
            labels=[
                "Score 0",
                "Score 1",
                "Score 2",
                "Score 3",
                "Score 4",
                "Score > 4",
            ],
        )

    # Contracción del rango
    if "range_ratio" in out.columns:
        out["range_ratio_bucket"] = pd.cut(
            out["range_ratio"],
            bins=[
                -np.inf,
                0.50,
                0.65,
                0.75,
                0.85,
                1.00,
                np.inf,
            ],
            labels=[
                "<= 0.50",
                "0.50 - 0.65",
                "0.65 - 0.75",
                "0.75 - 0.85",
                "0.85 - 1.00",
                "> 1.00",
            ],
        )

    # Contracción del ATR
    if "atr_ratio" in out.columns:
        out["atr_ratio_bucket"] = pd.cut(
            out["atr_ratio"],
            bins=[
                -np.inf,
                0.50,
                0.65,
                0.75,
                0.85,
                1.00,
                np.inf,
            ],
            labels=[
                "<= 0.50",
                "0.50 - 0.65",
                "0.65 - 0.75",
                "0.75 - 0.85",
                "0.85 - 1.00",
                "> 1.00",
            ],
        )

    # Comportamiento del volumen durante la compresión
    if "volume_ratio" in out.columns:
        out["volume_ratio_bucket"] = pd.cut(
            out["volume_ratio"],
            bins=[
                -np.inf,
                0.60,
                0.80,
                1.00,
                1.10,
                1.30,
                np.inf,
            ],
            labels=[
                "<= 0.60",
                "0.60 - 0.80",
                "0.80 - 1.00",
                "1.00 - 1.10",
                "1.10 - 1.30",
                "> 1.30",
            ],
        )

    # Cuerpo promedio respecto del rango de cada vela
    if "avg_body_pct" in out.columns:
        out["avg_body_pct_bucket"] = pd.cut(
            out["avg_body_pct"],
            bins=[
                -np.inf,
                0.25,
                0.35,
                0.45,
                0.55,
                0.70,
                np.inf,
            ],
            labels=[
                "<= 0.25",
                "0.25 - 0.35",
                "0.35 - 0.45",
                "0.45 - 0.55",
                "0.55 - 0.70",
                "> 0.70",
            ],
        )

    # Altura total del rango respecto del precio
    if "compression_range_pct" in out.columns:
        out["compression_range_pct_bucket"] = pd.cut(
            out["compression_range_pct"],
            bins=[
                -np.inf,
                0.50,
                0.75,
                1.00,
                1.50,
                2.00,
                3.00,
                5.00,
                np.inf,
            ],
            labels=[
                "<= 0.50%",
                "0.50% - 0.75%",
                "0.75% - 1.00%",
                "1.00% - 1.50%",
                "1.50% - 2.00%",
                "2.00% - 3.00%",
                "3.00% - 5.00%",
                "> 5.00%",
            ],
        )

    # =========================
    # COMPRESSION DURATION
    # =========================
    if "compression_duration" in out.columns:
        out["compression_duration_bucket"] = pd.cut(
            out["compression_duration"],
            bins=[-np.inf, 5, 10, 15, 20, 30, 50, np.inf],
            labels=[
                "<= 5",
                "6 - 10",
                "11 - 15",
                "16 - 20",
                "21 - 30",
                "31 - 50",
                "> 50",
            ],
        )

    # =========================
    # COMPRESSION HEIGHT %
    # =========================
    if "compression_height_pct" in out.columns:
        out["compression_height_bucket"] = pd.cut(
            out["compression_height_pct"],
            bins=[
                -np.inf,
                0.25,
                0.50,
                0.75,
                1.00,
                1.50,
                2.00,
                3.00,
                np.inf,
            ],
            labels=[
                "<= 0.25%",
                "0.25% - 0.50%",
                "0.50% - 0.75%",
                "0.75% - 1.00%",
                "1.00% - 1.50%",
                "1.50% - 2.00%",
                "2.00% - 3.00%",
                "> 3.00%",
            ],
        )

    # =========================
    # INSIDE RATIO
    # =========================
    if "inside_ratio" in out.columns:
        out["inside_ratio_bucket"] = pd.cut(
            out["inside_ratio"],
            bins=[
                -np.inf,
                0.50,
                0.60,
                0.70,
                0.80,
                0.90,
                0.95,
                np.inf,
            ],
            labels=[
                "<= 0.50",
                "0.50 - 0.60",
                "0.60 - 0.70",
                "0.70 - 0.80",
                "0.80 - 0.90",
                "0.90 - 0.95",
                "> 0.95",
            ],
        )

    # =========================
    # TOUCHES
    # =========================
    if "touches_high" in out.columns and "touches_low" in out.columns:
        out["total_touches"] = (
            out["touches_high"].fillna(0)
            + out["touches_low"].fillna(0)
        )

        out["total_touches_bucket"] = pd.cut(
            out["total_touches"],
            bins=[-np.inf, 2, 3, 4, 5, 6, 8, np.inf],
            labels=[
                "<= 2",
                "3",
                "4",
                "5",
                "6",
                "7 - 8",
                "> 8",
            ],
        )

        out["touch_balance"] = (
            out["touches_high"] - out["touches_low"]
        ).abs()
        
        out["touch_imbalance_signed"] = (
            out["touches_high"]
            - out["touches_low"]
        )

        out["touch_imbalance_direction"] = pd.cut(
            out["touch_imbalance_signed"],
            bins=[
                -np.inf,
                -2.5,
                -1.5,
                -0.5,
                0.5,
                1.5,
                2.5,
                np.inf,
            ],
            labels=[
                "More Low Touches > 2",
                "More Low Touches 2",
                "More Low Touches 1",
                "Balanced",
                "More High Touches 1",
                "More High Touches 2",
                "More High Touches > 2",
            ],
        )

        out["touch_balance_bucket"] = pd.cut(
            out["touch_balance"],
            bins=[-np.inf, 0, 1, 2, np.inf],
            labels=[
                "Balanced",
                "Difference 1",
                "Difference 2",
                "Difference > 2",
            ],
        )

    # =========================
    # SLOPE MAGNITUDE
    # =========================
    if "upper_slope" in out.columns:
        out["upper_slope_abs"] = out["upper_slope"].abs()

        out["upper_slope_bucket"] = pd.cut(
            out["upper_slope_abs"],
            bins=[-np.inf, 0.02, 0.05, 0.10, 0.20, np.inf],
            labels=[
                "Flat",
                "Very Low",
                "Low",
                "Medium",
                "High",
            ],
        )

    if "lower_slope" in out.columns:
        out["lower_slope_abs"] = out["lower_slope"].abs()

        out["lower_slope_bucket"] = pd.cut(
            out["lower_slope_abs"],
            bins=[-np.inf, 0.02, 0.05, 0.10, 0.20, np.inf],
            labels=[
                "Flat",
                "Very Low",
                "Low",
                "Medium",
                "High",
            ],
        )

    if "slope_difference" in out.columns:
        out["slope_difference_abs"] = out["slope_difference"].abs()

        out["slope_difference_bucket"] = pd.cut(
            out["slope_difference_abs"],
            bins=[-np.inf, 0.02, 0.05, 0.10, 0.20, np.inf],
            labels=[
                "Very Similar",
                "Similar",
                "Moderate Difference",
                "Large Difference",
                "Very Large Difference",
            ],
        )

    # =========================
    # BREAKOUT QUALITY
    # =========================

    # Volumen de la vela de breakout respecto del volumen de referencia
    if "breakout_volume_ratio" in out.columns:
        out["breakout_volume_ratio_bucket"] = pd.cut(
            out["breakout_volume_ratio"],
            bins=[
                -np.inf,
                0.75,
                1.00,
                1.25,
                1.50,
                2.00,
                3.00,
                np.inf,
            ],
            labels=[
                "<= 0.75x",
                "0.75x - 1.00x",
                "1.00x - 1.25x",
                "1.25x - 1.50x",
                "1.50x - 2.00x",
                "2.00x - 3.00x",
                "> 3.00x",
            ],
        )

    # Extensión porcentual alcanzada por el breakout
    if "breakout_extension_pct" in out.columns:
        out["breakout_extension_pct_bucket"] = pd.cut(
            out["breakout_extension_pct"],
            bins=[
                -np.inf,
                0,
                0.25,
                0.50,
                0.75,
                1.00,
                1.50,
                2.00,
                np.inf,
            ],
            labels=[
                "<= 0%",
                "0% - 0.25%",
                "0.25% - 0.50%",
                "0.50% - 0.75%",
                "0.75% - 1.00%",
                "1.00% - 1.50%",
                "1.50% - 2.00%",
                "> 2.00%",
            ],
        )

    # =========================
    # ENTRY LOCATION
    # =========================
    entry_bucket_configs = {
        "entry_distance_pct": "entry_distance_bucket_analytics",
        "entry_vs_compression_pct": "entry_vs_compression_bucket",
    }

    for source_col, bucket_col in entry_bucket_configs.items():
        if source_col not in out.columns:
            continue

        out[bucket_col] = pd.cut(
            out[source_col],
            bins=[
                -np.inf,
                0,
                0.10,
                0.25,
                0.50,
                0.75,
                1.00,
                1.50,
                2.00,
                np.inf,
            ],
            labels=[
                "< 0%",
                "0% - 0.10%",
                "0.10% - 0.25%",
                "0.25% - 0.50%",
                "0.50% - 0.75%",
                "0.75% - 1.00%",
                "1.00% - 1.50%",
                "1.50% - 2.00%",
                "> 2.00%",
            ],
        )

    # =========================
    # ENTRY VS BREAKOUT
    # =========================
    if "entry_vs_breakout_pct" in out.columns:
        out["entry_vs_breakout_bucket"] = pd.cut(
            out["entry_vs_breakout_pct"],
            bins=[
                -np.inf,
                -1.00,
                -0.50,
                -0.25,
                -0.10,
                0,
                0.10,
                0.25,
                0.50,
                0.75,
                1.00,
                1.50,
                2.00,
                np.inf,
            ],
            labels=[
                "< -1.00%",
                "-1.00% - -0.50%",
                "-0.50% - -0.25%",
                "-0.25% - -0.10%",
                "-0.10% - 0%",
                "0% - 0.10%",
                "0.10% - 0.25%",
                "0.25% - 0.50%",
                "0.50% - 0.75%",
                "0.75% - 1.00%",
                "1.00% - 1.50%",
                "1.50% - 2.00%",
                "> 2.00%",
            ],
        )

    # Normalize boolean for grouping
    if "late_entry" in out.columns:
        out["late_entry_label"] = (
            out["late_entry"]
            .astype(str)
            .str.lower()
            .map({
                "true": "Late Entry",
                "false": "Normal Entry",
                "1": "Late Entry",
                "0": "Normal Entry",
            })
            .fillna("Unknown")
        )

    return out


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

# =========================================================
# BTC CORRELATION ANALYTICS
# =========================================================

BTC_CORRELATION_TIMEFRAMES = ["15m", "1h", "4h"]


def btc_factor_profit_factor(series: pd.Series):
    pnl = pd.to_numeric(
        series,
        errors="coerce",
    ).dropna()

    gross_profit = pnl[pnl > 0].sum()
    gross_loss = abs(pnl[pnl < 0].sum())

    if gross_loss == 0:
        return None

    return round(gross_profit / gross_loss, 2)


def add_btc_correlation_buckets(
    data: pd.DataFrame,
    timeframe: str,
) -> pd.DataFrame:
    """
    Prepara las métricas y buckets de correlación BTC
    para un timeframe determinado.

    No modifica el dataframe original.
    """

    out = data.copy()

    corr_col = f"btc_corr_{timeframe}"
    beta_col = f"btc_beta_{timeframe}"
    r2_col = f"btc_r2_{timeframe}"

    symbol_move_col = f"symbol_move_{timeframe}_pct"
    btc_move_col = f"btc_move_{timeframe}_pct"

    expected_col = (
        f"btc_expected_move_{timeframe}_pct"
    )

    residual_col = (
        f"btc_residual_move_{timeframe}_pct"
    )

    numeric_columns = [
        corr_col,
        beta_col,
        r2_col,
        symbol_move_col,
        btc_move_col,
        expected_col,
        residual_col,
        "pnl",
        "pnl_usd",
        "max_favorable_pct",
        "max_adverse_pct",
    ]

    for column in numeric_columns:
        if column in out.columns:
            out[column] = pd.to_numeric(
                out[column],
                errors="coerce",
            )

    # =====================================================
    # RESIDUAL AJUSTADO AL LADO DEL TRADE
    # =====================================================

    directional_residual_col = (
        f"btc_directional_residual_{timeframe}_pct"
    )

    if (
        residual_col in out.columns
        and "side" in out.columns
    ):
        side_multiplier = np.where(
            out["side"]
            .astype(str)
            .str.upper()
            .eq("SHORT"),
            -1.0,
            1.0,
        )

        out[directional_residual_col] = (
            out[residual_col]
            * side_multiplier
        )

    else:
        out[directional_residual_col] = np.nan

    # =====================================================
    # CORRELATION BUCKETS
    # =====================================================

    if corr_col in out.columns:
        out[f"btc_corr_bucket_{timeframe}"] = pd.cut(
            out[corr_col],
            bins=[
                -np.inf,
                0.0,
                0.30,
                0.50,
                0.70,
                0.85,
                np.inf,
            ],
            labels=[
                "< 0",
                "0.00 - 0.30",
                "0.30 - 0.50",
                "0.50 - 0.70",
                "0.70 - 0.85",
                ">= 0.85",
            ],
            include_lowest=True,
            right=False,
        )

    # =====================================================
    # BETA BUCKETS
    # =====================================================

    if beta_col in out.columns:
        out[f"btc_beta_bucket_{timeframe}"] = pd.cut(
            out[beta_col],
            bins=[
                -np.inf,
                0.50,
                0.80,
                1.00,
                1.25,
                1.50,
                2.00,
                np.inf,
            ],
            labels=[
                "< 0.50",
                "0.50 - 0.80",
                "0.80 - 1.00",
                "1.00 - 1.25",
                "1.25 - 1.50",
                "1.50 - 2.00",
                ">= 2.00",
            ],
            include_lowest=True,
            right=False,
        )

    # =====================================================
    # R2 BUCKETS
    # =====================================================

    if r2_col in out.columns:
        out[f"btc_r2_bucket_{timeframe}"] = pd.cut(
            out[r2_col],
            bins=[
                -np.inf,
                0.20,
                0.40,
                0.60,
                0.80,
                np.inf,
            ],
            labels=[
                "< 0.20",
                "0.20 - 0.40",
                "0.40 - 0.60",
                "0.60 - 0.80",
                ">= 0.80",
            ],
            include_lowest=True,
            right=False,
        )

    # =====================================================
    # DIRECTIONAL RESIDUAL BUCKETS
    # =====================================================

    out[
        f"btc_directional_residual_bucket_{timeframe}"
    ] = pd.cut(
        out[directional_residual_col],
        bins=[
            -np.inf,
            -1.00,
            -0.50,
            -0.20,
            0.00,
            0.20,
            0.50,
            1.00,
            np.inf,
        ],
        labels=[
            "< -1.00%",
            "-1.00% - -0.50%",
            "-0.50% - -0.20%",
            "-0.20% - 0.00%",
            "0.00% - 0.20%",
            "0.20% - 0.50%",
            "0.50% - 1.00%",
            ">= 1.00%",
        ],
        include_lowest=True,
        right=False,
    )

    # =====================================================
    # BTC DEPENDENCY CLASSIFICATION
    # =====================================================

    dependency_col = f"btc_dependency_{timeframe}"

    def classify_dependency(row):
        corr = row.get(corr_col)
        r2 = row.get(r2_col)
        directional_residual = row.get(
            directional_residual_col
        )

        if (
            pd.isna(corr)
            or pd.isna(r2)
            or pd.isna(directional_residual)
        ):
            return "unknown"

        strongly_explained_by_btc = (
            corr >= 0.80
            and r2 >= 0.60
        )

        if strongly_explained_by_btc:
            if directional_residual <= -0.20:
                return "btc_copied_weak"

            if directional_residual < 0.20:
                return "btc_copied_neutral"

            return "btc_correlated_with_strength"

        if directional_residual >= 0.20:
            return "independent_strength"

        if directional_residual <= -0.20:
            return "independent_weakness"

        return "mixed"

    out[dependency_col] = out.apply(
        classify_dependency,
        axis=1,
    )

    return out


def build_btc_factor_report(
    data: pd.DataFrame,
    group_column: str,
    factor_column: str | None = None,
    min_trades: int = 1,
) -> pd.DataFrame:
    """
    Calcula performance por bucket o clasificación BTC.
    """

    required_columns = [
        group_column,
        "pnl",
    ]

    missing_columns = [
        column
        for column in required_columns
        if column not in data.columns
    ]

    if missing_columns:
        return pd.DataFrame()

    work = data.copy()

    work["pnl"] = pd.to_numeric(
        work["pnl"],
        errors="coerce",
    )

    for column in [
        factor_column,
        "pnl_usd",
        "max_favorable_pct",
        "max_adverse_pct",
    ]:
        if column and column in work.columns:
            work[column] = pd.to_numeric(
                work[column],
                errors="coerce",
            )

    work = work.dropna(
        subset=[group_column, "pnl"]
    )

    if work.empty:
        return pd.DataFrame()

    aggregations = {
        "trades": ("pnl", "count"),

        "wins": (
            "pnl",
            lambda values: int(
                (values > 0).sum()
            ),
        ),

        "losses": (
            "pnl",
            lambda values: int(
                (values <= 0).sum()
            ),
        ),

        "winrate": (
            "pnl",
            lambda values: round(
                (values > 0).mean() * 100,
                2,
            ),
        ),

        "avg_pnl": ("pnl", "mean"),
        "net_pnl": ("pnl", "sum"),
        "median_pnl": ("pnl", "median"),

        "avg_win": (
            "pnl",
            lambda values: (
                values[values > 0].mean()
                if (values > 0).any()
                else 0
            ),
        ),

        "avg_loss": (
            "pnl",
            lambda values: (
                values[values <= 0].mean()
                if (values <= 0).any()
                else 0
            ),
        ),

        "profit_factor": (
            "pnl",
            btc_factor_profit_factor,
        ),
    }

    if factor_column and factor_column in work.columns:
        aggregations["factor_avg"] = (
            factor_column,
            "mean",
        )

    if "pnl_usd" in work.columns:
        aggregations["net_pnl_usd"] = (
            "pnl_usd",
            "sum",
        )

    if "max_favorable_pct" in work.columns:
        aggregations["avg_mfe"] = (
            "max_favorable_pct",
            "mean",
        )

    if "max_adverse_pct" in work.columns:
        aggregations["avg_mae"] = (
            "max_adverse_pct",
            "mean",
        )

    report = (
        work
        .groupby(
            group_column,
            observed=True,
        )
        .agg(**aggregations)
        .reset_index()
    )

    report = report[
        report["trades"] >= min_trades
    ].copy()

    numeric_columns = [
        "avg_pnl",
        "net_pnl",
        "median_pnl",
        "avg_win",
        "avg_loss",
        "factor_avg",
        "net_pnl_usd",
        "avg_mfe",
        "avg_mae",
    ]

    for column in numeric_columns:
        if column in report.columns:
            report[column] = report[column].round(4)

    if report.empty:
        return report

    return report.sort_values(
        by=[
            "profit_factor",
            "net_pnl",
            "winrate",
            "trades",
        ],
        ascending=[
            False,
            False,
            False,
            False,
        ],
        na_position="last",
    )

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

@st.cache_data(ttl=2)
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
            "trigger_tf": raw.get("trigger_tf", "N/A"),
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
df = load_csv_cached(TRADES_FILE)
paper_df = load_csv_cached(PAPER_SIGNALS_FILE)

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

compression_numeric_cols = [
    # Prices / execution
    "real_entry",
    "compression_high",
    "compression_low",
    "breakout_price",

    # Breakout
    "breakout_extension_pct",
    "breakout_extension_atr",
    "breakout_volume_ratio",

    # Existing scores
    "compression_score",
    "trend_score",

    # Compression structure
    "compression_height",
    "compression_height_pct",
    "compression_duration",
    "upper_slope",
    "lower_slope",
    "slope_difference",
    "touches_high",
    "touches_low",
    "inside_ratio",

    # Entry location
    "entry_distance_pct",
    "entry_vs_compression_pct",
    "entry_vs_breakout_pct",
    "entry_to_compression_low_pct",
    "sl_to_compression_low_pct",

    # Trade result
    "pnl",
    "pnl_usd",
    "max_favorable_pct",
    "max_adverse_pct",
]

for col in compression_numeric_cols:
    if col in df_raw.columns:
        df_raw[col] = pd.to_numeric(df_raw[col], errors="coerce")
        
# =========================
# BTC CORRELATION NUMERIC
# =========================

btc_correlation_numeric_cols = []

for timeframe in BTC_CORRELATION_TIMEFRAMES:
    btc_correlation_numeric_cols.extend([
        f"btc_corr_{timeframe}",
        f"btc_beta_{timeframe}",
        f"btc_r2_{timeframe}",

        f"symbol_move_{timeframe}_pct",
        f"btc_move_{timeframe}_pct",

        f"btc_expected_move_{timeframe}_pct",
        f"btc_residual_move_{timeframe}_pct",
    ])

for col in btc_correlation_numeric_cols:
    if col in df_raw.columns:
        df_raw[col] = pd.to_numeric(
            df_raw[col],
            errors="coerce",
        )

required_compression_cols = [
    "side",
    "real_entry",
    "compression_high",
    "compression_low",
    "breakout_price",
]

if all(col in df_raw.columns for col in required_compression_cols):

    df_raw["entry_vs_compression_pct"] = np.where(
        df_raw["side"].astype(str).str.upper() == "LONG",
        (
            (df_raw["real_entry"] - df_raw["compression_high"])
            / df_raw["compression_high"]
            * 100
        ),
        (
            (df_raw["compression_low"] - df_raw["real_entry"])
            / df_raw["compression_low"]
            * 100
        )
    )

    df_raw["entry_vs_breakout_pct"] = np.where(
        df_raw["side"].astype(str).str.upper() == "LONG",
        (
            (df_raw["real_entry"] - df_raw["breakout_price"])
            / df_raw["breakout_price"]
            * 100
        ),
        (
            (df_raw["breakout_price"] - df_raw["real_entry"])
            / df_raw["breakout_price"]
            * 100
        )
    )

    df_raw["entry_vs_compression_pct"] = df_raw["entry_vs_compression_pct"].round(4)
    df_raw["entry_vs_breakout_pct"] = df_raw["entry_vs_breakout_pct"].round(4)

    df_raw["late_entry"] = df_raw["entry_vs_compression_pct"] > 1.0
    
    # =========================
    # COMPRESSION STOP ANALYSIS
    # =========================

    df_raw["compression_height"] = (
        df_raw["compression_high"] - df_raw["compression_low"]
    )

    df_raw["entry_to_compression_low_pct"] = np.where(
        df_raw["side"].str.upper() == "LONG",

        (
            (df_raw["real_entry"] - df_raw["compression_low"])
            / df_raw["compression_low"]
            * 100
        ),

        (
            (df_raw["compression_high"] - df_raw["real_entry"])
            / df_raw["compression_high"]
            * 100
        )
    )

    df_raw["sl_to_compression_low_pct"] = np.where(
        df_raw["side"].str.upper() == "LONG",

        (
            (df_raw["compression_low"] - df_raw["sl"])
            / df_raw["compression_low"]
            * 100
        ),

        (
            (df_raw["sl"] - df_raw["compression_high"])
            / df_raw["compression_high"]
            * 100
        )
    )
else:
    df_raw["entry_vs_compression_pct"] = np.nan
    df_raw["entry_vs_breakout_pct"] = np.nan
    df_raw["late_entry"] = False

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
@st.cache_data(ttl=30)
def build_mfe_mae_report_cached(df):
    return build_mfe_mae_report(df)

mfe_report = build_mfe_mae_report_cached(df_view)

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

trigger_tf = status.get("trigger_tf", "N/A")

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
            "STRATEGY / TF",
            f"{strategy_mode} / {trigger_tf}"
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
    
(
    tab_overview,
    tab_btc_correlation,
    tab_btc_alignment_edge,
    tab_mfe_mae,
    tab_setups,
    tab_swings,
    tab_bad_decisions,
    tab_execution,
    tab_compression_quality,
    tab_compression_analytics,
    tab_compression_pipeline,
    tab_tp_sl_replay,
) = st.tabs([
    "📊 Overview",
    "₿ BTC Correlation",
    "🧭 BTC Alignment Edge",
    "📐 MFE / MAE",
    "🧠 Setups",
    "🎯 Swings",
    "❌ Bad Decisions x",
    "⏱️ Execution Analysis",
    "🎯 Compression Entry Quality",
    "🔬 Compression Analytics",
    "Compression Pipeline",
    "🧪 TP / SL Replay",
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

    overview_metrics = calculate_metrics(df_view.to_dict("records"))

    net_pnl_usd = safe_sum(df_view, "pnl_usd")
    best_trade = safe_max(df_view, "pnl")

    st.markdown("### 📊 Performance")

    col1, col2, col3, col4, col5, col6 = st.columns(6)

    col1.metric("Trades", overview_metrics["trades"])
    col2.metric("Net PnL %", f'{overview_metrics["net_pnl"]}%')
    col3.metric("Net PnL USD", f"{net_pnl_usd} USDT")
    col4.metric("Winrate", f'{overview_metrics["winrate"]}%')
    col5.metric("Profit Factor", overview_metrics["profit_factor"])
    col6.metric("Expectancy", overview_metrics["expectancy"])

    col7, col8, col9, col10, col11, col12 = st.columns(6)

    col7.metric("Gross PnL", overview_metrics["gross_pnl"])
    col8.metric("Fees", overview_metrics["fees"])
    col9.metric("Fees / Trade", overview_metrics["fees_per_trade"])
    col10.metric("Avg Win", overview_metrics["avg_win"])
    col11.metric("Avg Loss", overview_metrics["avg_loss"])
    col12.metric("Max DD", overview_metrics["max_drawdown"])

    col13, col14, col15, col16 = st.columns(4)

    col13.metric("Best Trade %", best_trade)
    col14.metric("Fee Impact", overview_metrics["fee_impact"])
    col15.metric("Fee Drag", overview_metrics["fee_drag"])
    col16.metric("Fee / Avg Win", overview_metrics["fee_to_avg_win"])
        
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
        table_df = table_df.sort_values(
            "entry_ts_dt",
            ascending=False,
        )

    # ==================================================
    # RAW ROWS ALIGNED WITH THE DISPLAYED TABLE
    # ==================================================
    # table_df después será formateado para visualización.
    # inspector_source_df conserva tipos, timestamps y números reales.
    inspector_source_df = (
        df_view
        .loc[table_df.index]
        .copy()
        .reset_index(drop=True)
    )

    table_df = table_df.reset_index(drop=True)

    if "entry_distance_pct" in table_df.columns:
        table_df["entry_distance_pct"] = table_df["entry_distance_pct"].map(
            lambda x: f"{x:.4f}%" if pd.notnull(x) else "0.0000%"
        )
    
    # NUEVO
    for pct_col in [
        "entry_vs_compression_pct",
        "entry_vs_breakout_pct",
        "breakout_extension_pct",
        "breakout_extension_atr",
        "breakout_volume_ratio",
    ]:
        if pct_col in table_df.columns:
            table_df[pct_col] = table_df[pct_col].map(
                lambda x: f"{x:.4f}" if pd.notnull(x) else "-"
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
        "compression_high",
        "compression_low",
        "breakout_price",
        "entry_ready_price",
    ]

    for col in price_cols:
        if col in table_df.columns:
            table_df[col] = table_df[col].map(fmt_price_for_display)
            
    st.caption(
        f"Showing {len(table_df)} trades "
        f"from {start_date} to {end_date}"
    )

    closed_trades_event = st.dataframe(
        table_df,
        use_container_width=True,
        hide_index=True,
        key="closed_trades_inspector_table",
        on_select="rerun",
        selection_mode="single-row",
    )

    selected_closed_rows = (
        closed_trades_event.selection.rows
    )

    if selected_closed_rows:
        selected_position = selected_closed_rows[0]

        selected_trade_row = inspector_source_df.iloc[
            selected_position
        ]

        closed_inspection = trade_inspector_service.inspect(
            row=selected_trade_row,
            status="CLOSED",
        )

        selected_trade = closed_inspection.trade
        
        inspector_timeframe = trigger_tf

        valid_inspector_timeframes = {
            "1m",
            "3m",
            "5m",
            "15m",
            "30m",
            "1h",
            "2h",
            "4h",
        }

        if inspector_timeframe not in valid_inspector_timeframes:
            inspector_timeframe = "30m"

        inspector_interval_minutes = timeframe_to_minutes(
            inspector_timeframe
        )
        
        with st.spinner(
            f"Loading {inspector_timeframe} candles "
            f"for {selected_trade.symbol}..."
        ):
            closed_inspection.candles = (
                trade_inspector_service.load_trade_candles(
                    trade=selected_trade,
                    interval=inspector_timeframe,
                    candles_before=20,
                    candles_after=12,
                )
            )

        st.markdown("---")

        st.subheader(
            f"🔎 Trade Inspector — {selected_trade.symbol}"
        )
        
        st.caption(
            f"Inspection timeframe: {inspector_timeframe}"
        )

        # =========================
        # MAIN TRADE DATA
        # =========================

        c1, c2, c3, c4 = st.columns(4)

        c1.metric(
            "Status",
            selected_trade.status or "-",
        )

        c2.metric(
            "Side",
            selected_trade.side or "-",
        )

        c3.metric(
            "Entry",
            fmt_price_for_display(
                selected_trade.entry_price
            ),
        )

        c4.metric(
            "Exit",
            fmt_price_for_display(
                selected_trade.exit_price
            ),
        )

        # =========================
        # COMPRESSION DATA
        # =========================

        c5, c6, c7, c8 = st.columns(4)

        c5.metric(
            "Compression High",
            fmt_price_for_display(
                selected_trade.compression_high
            ),
        )

        c6.metric(
            "Compression Low",
            fmt_price_for_display(
                selected_trade.compression_low
            ),
        )

        c7.metric(
            "Breakout",
            fmt_price_for_display(
                selected_trade.breakout_price
            ),
        )

        c8.metric(
            "Entry Ready",
            fmt_price_for_display(
                selected_trade.entry_ready_price
            ),
        )
        
        candles_df = closed_inspection.candles

        if candles_df.empty:
            st.warning(
                "No se pudieron cargar las velas 30m "
                "para el trade seleccionado."
            )

        else:
            first_candle = candles_df.iloc[0]
            last_candle = candles_df.iloc[-1]

            st.success(
                f"Loaded {len(candles_df)} candles — "
                f"{first_candle['open_ts']} → "
                f"{last_candle['close_ts']}"
            )

            inspector_fig = build_trade_inspector_chart(
                inspection=closed_inspection,
                interval_minutes=inspector_interval_minutes,
                timeframe=inspector_timeframe,
            )

            st.plotly_chart(
                inspector_fig,
                use_container_width=True,
                config={
                    "displaylogo": False,
                    "scrollZoom": True,
                },
            )

            with st.expander(
                f"{inspector_timeframe} candles",
                expanded=False,
            ):
                st.dataframe(
                    candles_df[
                        [
                            "open_ts",
                            "open",
                            "high",
                            "low",
                            "close",
                            "volume",
                        ]
                    ],
                    use_container_width=True,
                    hide_index=True,
                )

        # =========================
        # NORMALIZED DEBUG DATA
        # =========================

        with st.expander(
            "Normalized trade data",
            expanded=False,
        ):
            st.json({
                "symbol": selected_trade.symbol,
                "status": selected_trade.status,
                "side": selected_trade.side,

                "entry_ts": (
                    selected_trade.entry_ts.isoformat()
                    if selected_trade.entry_ts is not None
                    else None
                ),

                "entry_price": selected_trade.entry_price,

                "exit_ts": (
                    selected_trade.exit_ts.isoformat()
                    if selected_trade.exit_ts is not None
                    else None
                ),

                "exit_price": selected_trade.exit_price,
                "exit_reason": selected_trade.exit_reason,

                "compression_start_ts": (
                    selected_trade.compression_start_ts.isoformat()
                    if selected_trade.compression_start_ts is not None
                    else None
                ),

                "compression_created_ts": (
                    selected_trade.compression_created_ts.isoformat()
                    if selected_trade.compression_created_ts is not None
                    else None
                ),

                "compression_updated_ts": (
                    selected_trade.compression_updated_ts.isoformat()
                    if selected_trade.compression_updated_ts is not None
                    else None
                ),

                "compression_high": (
                    selected_trade.compression_high
                ),

                "compression_low": (
                    selected_trade.compression_low
                ),

                "compression_score": (
                    selected_trade.compression_score
                ),

                "breakout_ts": (
                    selected_trade.breakout_ts.isoformat()
                    if selected_trade.breakout_ts is not None
                    else None
                ),

                "breakout_price": (
                    selected_trade.breakout_price
                ),

                "breakout_high": (
                    selected_trade.breakout_high
                ),

                "pullback_first_ts": (
                    selected_trade.pullback_first_ts.isoformat()
                    if selected_trade.pullback_first_ts is not None
                    else None
                ),

                "pullback_valid_ts": (
                    selected_trade.pullback_valid_ts.isoformat()
                    if selected_trade.pullback_valid_ts is not None
                    else None
                ),

                "pullback_price": (
                    selected_trade.pullback_price
                ),

                "entry_ready_ts": (
                    selected_trade.entry_ready_ts.isoformat()
                    if selected_trade.entry_ready_ts is not None
                    else None
                ),

                "entry_ready_price": (
                    selected_trade.entry_ready_price
                ),

                "tp": selected_trade.tp,
                "sl": selected_trade.sl,
                "pnl_pct": selected_trade.pnl_pct,
                "pnl_usd": selected_trade.pnl_usd,
            })
        
    # =========================
    # EQUITY CURVE USD
    # =========================

    if "entry_ts_dt" in df_raw.columns and "pnl_usd" in df_raw.columns:

        st.markdown("---")
        st.subheader("💵 Equity Curve USD")

        df_equity_usd = (
            df_raw
            .dropna(subset=["entry_ts_dt", "pnl_usd"])
            .sort_values("entry_ts_dt")
            .copy()
        )

        if not df_equity_usd.empty:
            df_equity_usd["equity_usd"] = df_equity_usd["pnl_usd"].cumsum()

            st.line_chart(
                df_equity_usd.set_index("entry_ts_dt")["equity_usd"],
                use_container_width=True,
            )
        else:
            st.info("No USD equity data.")
            
# =========================================================
# BTC CORRELATION TAB
# =========================================================

with tab_btc_correlation:

    st.markdown("## ₿ BTC Correlation Analytics")

    st.caption(
        "Cruza correlación, beta, R² y movimiento residual "
        "contra el rendimiento real de los trades. "
        "Esta tab todavía no bloquea operaciones."
    )

    # =====================================================
    # CONTROLS
    # =====================================================

    control_col_1, control_col_2 = st.columns(2)

    with control_col_1:
        selected_btc_tf = st.selectbox(
            "BTC correlation timeframe",
            options=BTC_CORRELATION_TIMEFRAMES,
            index=0,
            key="btc_correlation_selected_tf",
        )

    with control_col_2:
        max_bucket_trades = max(
            1,
            min(100, len(df_view)),
        )

        default_min_trades = min(
            10,
            max_bucket_trades,
        )

        min_btc_bucket_trades = st.number_input(
            "Minimum trades per group",
            min_value=1,
            max_value=max_bucket_trades,
            value=default_min_trades,
            step=1,
            key="btc_correlation_min_trades",
        )

    corr_col = f"btc_corr_{selected_btc_tf}"
    beta_col = f"btc_beta_{selected_btc_tf}"
    r2_col = f"btc_r2_{selected_btc_tf}"

    symbol_move_col = (
        f"symbol_move_{selected_btc_tf}_pct"
    )

    btc_move_col = (
        f"btc_move_{selected_btc_tf}_pct"
    )

    expected_col = (
        f"btc_expected_move_{selected_btc_tf}_pct"
    )

    residual_col = (
        f"btc_residual_move_{selected_btc_tf}_pct"
    )

    directional_residual_col = (
        f"btc_directional_residual_"
        f"{selected_btc_tf}_pct"
    )

    corr_bucket_col = (
        f"btc_corr_bucket_{selected_btc_tf}"
    )

    beta_bucket_col = (
        f"btc_beta_bucket_{selected_btc_tf}"
    )

    r2_bucket_col = (
        f"btc_r2_bucket_{selected_btc_tf}"
    )

    residual_bucket_col = (
        f"btc_directional_residual_bucket_"
        f"{selected_btc_tf}"
    )

    dependency_col = (
        f"btc_dependency_{selected_btc_tf}"
    )

    required_btc_columns = [
        corr_col,
        beta_col,
        r2_col,
        residual_col,
        "pnl",
    ]

    missing_btc_columns = [
        column
        for column in required_btc_columns
        if column not in df_view.columns
    ]

    if missing_btc_columns:
        st.info(
            "Todavía faltan columnas de BTC correlation "
            f"para {selected_btc_tf}: "
            f"{', '.join(missing_btc_columns)}"
        )

    else:
        btc_analysis_df = add_btc_correlation_buckets(
            data=df_view,
            timeframe=selected_btc_tf,
        )

        available_df = btc_analysis_df.dropna(
            subset=[
                corr_col,
                beta_col,
                r2_col,
                residual_col,
                "pnl",
            ]
        ).copy()

        if available_df.empty:
            st.info(
                "Las columnas existen, pero todavía no hay "
                "trades con métricas completas."
            )

        else:
            # =================================================
            # SUMMARY CARDS
            # =================================================

            st.markdown("### Summary")

            summary_cols = st.columns(6)

            summary_cols[0].metric(
                "Trades analyzed",
                len(available_df),
            )

            summary_cols[1].metric(
                f"Avg Corr {selected_btc_tf}",
                f"{available_df[corr_col].mean():.3f}",
            )

            summary_cols[2].metric(
                f"Avg Beta {selected_btc_tf}",
                f"{available_df[beta_col].mean():.3f}",
            )

            summary_cols[3].metric(
                f"Avg R² {selected_btc_tf}",
                f"{available_df[r2_col].mean():.3f}",
            )

            summary_cols[4].metric(
                "Avg residual",
                (
                    f"{available_df[residual_col].mean():.3f}%"
                ),
            )

            avg_directional_residual = (
                available_df[
                    directional_residual_col
                ].mean()
            )

            summary_cols[5].metric(
                "Avg directional residual",
                f"{avg_directional_residual:.3f}%",
            )

            st.caption(
                "Directional residual está ajustado al lado "
                "del trade: positivo favorece la operación; "
                "negativo muestra debilidad relativa."
            )

            # =================================================
            # BTC DEPENDENCY CLASSIFICATION
            # =================================================

            st.markdown("---")
            st.markdown(
                "### BTC Dependency Classification"
            )

            dependency_report = build_btc_factor_report(
                data=available_df,
                group_column=dependency_col,
                factor_column=directional_residual_col,
                min_trades=int(
                    min_btc_bucket_trades
                ),
            )

            if dependency_report.empty:
                st.info(
                    "No hay suficientes trades por "
                    "clasificación."
                )

            else:
                st.dataframe(
                    dependency_report,
                    use_container_width=True,
                    hide_index=True,
                )

                dependency_chart = (
                    dependency_report[
                        [
                            dependency_col,
                            "profit_factor",
                        ]
                    ]
                    .dropna(subset=["profit_factor"])
                    .set_index(dependency_col)
                )

                if not dependency_chart.empty:
                    st.bar_chart(
                        dependency_chart,
                        use_container_width=True,
                    )

            st.caption(
                "btc_copied_weak: BTC explica mucho del movimiento "
                "y el residual es contrario al trade. "
                "btc_correlated_with_strength: correlación alta, "
                "pero el activo además muestra fuerza propia."
            )

            # =================================================
            # CORRELATION BUCKETS
            # =================================================

            st.markdown("---")
            st.markdown("### Correlation Buckets")

            correlation_report = build_btc_factor_report(
                data=available_df,
                group_column=corr_bucket_col,
                factor_column=corr_col,
                min_trades=int(
                    min_btc_bucket_trades
                ),
            )

            if correlation_report.empty:
                st.info(
                    "No hay suficientes trades por bucket "
                    "de correlación."
                )

            else:
                st.dataframe(
                    correlation_report,
                    use_container_width=True,
                    hide_index=True,
                )

            # =================================================
            # BETA BUCKETS
            # =================================================

            st.markdown("---")
            st.markdown("### Beta Buckets")

            beta_report = build_btc_factor_report(
                data=available_df,
                group_column=beta_bucket_col,
                factor_column=beta_col,
                min_trades=int(
                    min_btc_bucket_trades
                ),
            )

            if beta_report.empty:
                st.info(
                    "No hay suficientes trades por bucket "
                    "de beta."
                )

            else:
                st.dataframe(
                    beta_report,
                    use_container_width=True,
                    hide_index=True,
                )

            # =================================================
            # R2 BUCKETS
            # =================================================

            st.markdown("---")
            st.markdown("### R² Buckets")

            r2_report = build_btc_factor_report(
                data=available_df,
                group_column=r2_bucket_col,
                factor_column=r2_col,
                min_trades=int(
                    min_btc_bucket_trades
                ),
            )

            if r2_report.empty:
                st.info(
                    "No hay suficientes trades por bucket "
                    "de R²."
                )

            else:
                st.dataframe(
                    r2_report,
                    use_container_width=True,
                    hide_index=True,
                )

            # =================================================
            # DIRECTIONAL RESIDUAL
            # =================================================

            st.markdown("---")
            st.markdown(
                "### Directional Residual Buckets"
            )

            residual_report = build_btc_factor_report(
                data=available_df,
                group_column=residual_bucket_col,
                factor_column=directional_residual_col,
                min_trades=int(
                    min_btc_bucket_trades
                ),
            )

            if residual_report.empty:
                st.info(
                    "No hay suficientes trades por bucket "
                    "de movimiento residual."
                )

            else:
                st.dataframe(
                    residual_report,
                    use_container_width=True,
                    hide_index=True,
                )

            # =================================================
            # CORRELATION + RESIDUAL MATRIX
            # =================================================

            st.markdown("---")
            st.markdown(
                "### Correlation × Directional Residual"
            )

            correlation_residual_report = (
                build_btc_factor_report(
                    data=available_df,
                    group_column=corr_bucket_col,
                    factor_column=corr_col,
                    min_trades=1,
                )
            )

            matrix_work = available_df.dropna(
                subset=[
                    corr_bucket_col,
                    residual_bucket_col,
                    "pnl",
                ]
            ).copy()

            if matrix_work.empty:
                st.info(
                    "No hay datos para construir la matriz."
                )

            else:
                matrix_rows = []

                grouped_matrix = matrix_work.groupby(
                    [
                        corr_bucket_col,
                        residual_bucket_col,
                    ],
                    observed=True,
                )

                for (
                    corr_bucket,
                    residual_bucket,
                ), group in grouped_matrix:

                    pnl_values = pd.to_numeric(
                        group["pnl"],
                        errors="coerce",
                    ).dropna()

                    if pnl_values.empty:
                        continue

                    matrix_rows.append({
                        "correlation_bucket": str(
                            corr_bucket
                        ),

                        "directional_residual_bucket": str(
                            residual_bucket
                        ),

                        "trades": len(pnl_values),

                        "winrate": round(
                            (
                                pnl_values > 0
                            ).mean() * 100,
                            2,
                        ),

                        "avg_pnl": round(
                            pnl_values.mean(),
                            4,
                        ),

                        "net_pnl": round(
                            pnl_values.sum(),
                            4,
                        ),

                        "profit_factor": (
                            btc_factor_profit_factor(
                                pnl_values
                            )
                        ),
                    })

                matrix_report = pd.DataFrame(
                    matrix_rows
                )

                if not matrix_report.empty:
                    matrix_report = matrix_report[
                        matrix_report["trades"]
                        >= int(min_btc_bucket_trades)
                    ]

                if matrix_report.empty:
                    st.info(
                        "No hay combinaciones con la muestra "
                        "mínima seleccionada."
                    )

                else:
                    matrix_report = (
                        matrix_report.sort_values(
                            by=[
                                "profit_factor",
                                "net_pnl",
                                "trades",
                            ],
                            ascending=[
                                False,
                                False,
                                False,
                            ],
                            na_position="last",
                        )
                    )

                    st.dataframe(
                        matrix_report,
                        use_container_width=True,
                        hide_index=True,
                    )

                    pf_pivot = matrix_report.pivot_table(
                        index="correlation_bucket",
                        columns=(
                            "directional_residual_bucket"
                        ),
                        values="profit_factor",
                        aggfunc="first",
                    )

                    if not pf_pivot.empty:
                        st.markdown(
                            "#### Profit Factor Matrix"
                        )

                        st.dataframe(
                            pf_pivot,
                            use_container_width=True,
                        )

            # =================================================
            # RAW CORRELATION TRADES
            # =================================================

            st.markdown("---")
            st.markdown(
                "### Trades With BTC Correlation Data"
            )

            raw_columns = [
                "entry_ts",
                "symbol",
                "side",
                "pnl",
                "pnl_usd",
                corr_col,
                beta_col,
                r2_col,
                symbol_move_col,
                btc_move_col,
                expected_col,
                residual_col,
                directional_residual_col,
                dependency_col,
                "max_favorable_pct",
                "max_adverse_pct",
                "exit_reason",
            ]

            existing_raw_columns = [
                column
                for column in raw_columns
                if column in available_df.columns
            ]

            raw_btc_table = available_df[
                existing_raw_columns
            ].copy()

            if (
                "entry_ts_dt"
                in available_df.columns
            ):
                raw_btc_table.insert(
                    0,
                    "entry_time",
                    available_df[
                        "entry_ts_dt"
                    ].dt.strftime(
                        "%d-%m-%Y %H:%M"
                    ),
                )

            st.dataframe(
                raw_btc_table,
                use_container_width=True,
                hide_index=True,
            )
            
# =========================================================
# BTC ALIGNMENT EDGE TAB
# =========================================================

with tab_btc_alignment_edge:

    st.markdown("## 🧭 BTC Alignment Edge")

    st.caption(
        "Busca edge cruzando dirección, velocidad, correlación y beta "
        "de BTC con compresión, señales, swings, volumen y calidad "
        "de entrada. Todos los resultados respetan el filtro global "
        "de fechas."
    )

    # =====================================================
    # HELPERS
    # =====================================================

    def btc_edge_profit_factor(series):
        values = pd.to_numeric(
            series,
            errors="coerce",
        ).dropna()

        gross_profit = values[values > 0].sum()
        gross_loss = abs(values[values < 0].sum())

        if gross_loss <= 0:
            return None

        return round(
            gross_profit / gross_loss,
            3,
        )

    def build_btc_edge_report(
        data,
        group_columns,
        minimum_trades=1,
    ):
        if isinstance(group_columns, str):
            group_columns = [group_columns]

        required = group_columns + ["pnl"]

        if any(
            column not in data.columns
            for column in required
        ):
            return pd.DataFrame()

        work = data.copy()

        work["pnl"] = pd.to_numeric(
            work["pnl"],
            errors="coerce",
        )

        work = work.dropna(
            subset=required
        )

        if work.empty:
            return pd.DataFrame()

        aggregations = {
            "trades": ("pnl", "count"),
            "wins": (
                "pnl",
                lambda values: int(
                    (values > 0).sum()
                ),
            ),
            "losses": (
                "pnl",
                lambda values: int(
                    (values <= 0).sum()
                ),
            ),
            "winrate": (
                "pnl",
                lambda values: round(
                    (values > 0).mean() * 100,
                    2,
                ),
            ),
            "avg_pnl": ("pnl", "mean"),
            "median_pnl": ("pnl", "median"),
            "net_pnl": ("pnl", "sum"),
            "avg_win": (
                "pnl",
                lambda values: (
                    values[values > 0].mean()
                    if (values > 0).any()
                    else 0
                ),
            ),
            "avg_loss": (
                "pnl",
                lambda values: (
                    values[values <= 0].mean()
                    if (values <= 0).any()
                    else 0
                ),
            ),
            "profit_factor": (
                "pnl",
                btc_edge_profit_factor,
            ),
        }

        if "max_favorable_pct" in work.columns:
            aggregations["avg_mfe"] = (
                "max_favorable_pct",
                "mean",
            )

        if "max_adverse_pct" in work.columns:
            aggregations["avg_mae"] = (
                "max_adverse_pct",
                "mean",
            )

        report = (
            work
            .groupby(
                group_columns,
                observed=True,
            )
            .agg(**aggregations)
            .reset_index()
        )

        report = report[
            report["trades"] >= minimum_trades
        ].copy()

        numeric_columns = [
            "avg_pnl",
            "median_pnl",
            "net_pnl",
            "avg_win",
            "avg_loss",
            "avg_mfe",
            "avg_mae",
        ]

        for column in numeric_columns:
            if column in report.columns:
                report[column] = (
                    pd.to_numeric(
                        report[column],
                        errors="coerce",
                    )
                    .round(4)
                )

        if report.empty:
            return report

        return report.sort_values(
            by=[
                "profit_factor",
                "net_pnl",
                "winrate",
                "trades",
            ],
            ascending=[
                False,
                False,
                False,
                False,
            ],
            na_position="last",
        )

    # =====================================================
    # PREPARE DATA
    # =====================================================

    btc_edge_df = df_view.copy()

    numeric_columns = [
        "pnl",
        "pnl_usd",
        "max_favorable_pct",
        "max_adverse_pct",

        "btc_corr_5m_1h",
        "btc_beta_5m_1h",
        "btc_r2_5m_1h",

        "btc_corr_5m_4h",
        "btc_beta_5m_4h",
        "btc_r2_5m_4h",

        "btc_corr_5m_24h",
        "btc_beta_5m_24h",
        "btc_r2_5m_24h",

        "btc_signed_move_15m_pct",
        "btc_signed_move_1h_pct",

        "compression_score",
        "trend_score",
        "breakout_extension_atr",
        "breakout_extension_pct",
        "breakout_volume_ratio",
        "entry_vs_compression_pct",
        "entry_vs_breakout_pct",
        "relative_volume_15m",
        "relative_volume_1h",
    ]

    for column in numeric_columns:
        if column in btc_edge_df.columns:
            btc_edge_df[column] = pd.to_numeric(
                btc_edge_df[column],
                errors="coerce",
            )

    # =====================================================
    # CORRELATION BUCKET
    # =====================================================

    if "btc_corr_5m_1h" in btc_edge_df.columns:
        btc_edge_df["btc_corr_5m_1h_bucket"] = pd.cut(
            btc_edge_df["btc_corr_5m_1h"],
            bins=[
                -np.inf,
                0,
                0.20,
                0.40,
                0.60,
                0.80,
                np.inf,
            ],
            labels=[
                "1. Inverse (< 0)",
                "2. Not following (0-0.20)",
                "3. Weak (0.20-0.40)",
                "4. Moderate (0.40-0.60)",
                "5. Following (0.60-0.80)",
                "6. Strong following (>= 0.80)",
            ],
            include_lowest=True,
            right=False,
        )

    # =====================================================
    # BETA BUCKET
    # =====================================================

    if "btc_beta_5m_1h" in btc_edge_df.columns:
        btc_edge_df["btc_beta_5m_1h_bucket"] = pd.cut(
            btc_edge_df["btc_beta_5m_1h"],
            bins=[
                -np.inf,
                0,
                0.50,
                0.70,
                1.20,
                2.00,
                np.inf,
            ],
            labels=[
                "1. Inverse (< 0)",
                "2. Low (0-0.50)",
                "3. Medium (0.50-0.70)",
                "4. Normal (0.70-1.20)",
                "5. Amplified (1.20-2.00)",
                "6. Extreme (>= 2.00)",
            ],
            include_lowest=True,
            right=False,
        )

    # =====================================================
    # SIGNED BTC MOVEMENT BUCKETS
    # =====================================================

    if "btc_signed_move_15m_pct" in btc_edge_df.columns:
        btc_edge_df["btc_move_15m_bucket"] = pd.cut(
            btc_edge_df["btc_signed_move_15m_pct"],
            bins=[
                -np.inf,
                -0.80,
                -0.50,
                -0.15,
                0.15,
                0.50,
                0.80,
                np.inf,
            ],
            labels=[
                "1. Strong down",
                "2. Danger down",
                "3. Mild down",
                "4. Flat",
                "5. Mild up",
                "6. Danger up",
                "7. Strong up",
            ],
            include_lowest=True,
            right=False,
        )

    if "btc_signed_move_1h_pct" in btc_edge_df.columns:
        btc_edge_df["btc_move_1h_bucket"] = pd.cut(
            btc_edge_df["btc_signed_move_1h_pct"],
            bins=[
                -np.inf,
                -1.50,
                -1.00,
                -0.30,
                0.30,
                1.00,
                1.50,
                np.inf,
            ],
            labels=[
                "1. Strong down",
                "2. Danger down",
                "3. Mild down",
                "4. Flat",
                "5. Mild up",
                "6. Danger up",
                "7. Strong up",
            ],
            include_lowest=True,
            right=False,
        )

    # =====================================================
    # COMPRESSION / ENTRY BUCKETS
    # =====================================================

    if "compression_score" in btc_edge_df.columns:
        btc_edge_df["btc_edge_compression_score_bucket"] = pd.cut(
            btc_edge_df["compression_score"],
            bins=[
                -np.inf,
                3,
                4,
                5,
                np.inf,
            ],
            labels=[
                "< 3",
                "3",
                "4",
                ">= 5",
            ],
            include_lowest=True,
            right=False,
        )

    if "trend_score" in btc_edge_df.columns:
        btc_edge_df["btc_edge_trend_score_bucket"] = pd.cut(
            btc_edge_df["trend_score"],
            bins=[
                -np.inf,
                3,
                4,
                5,
                6,
                np.inf,
            ],
            labels=[
                "< 3",
                "3",
                "4",
                "5",
                ">= 6",
            ],
            include_lowest=True,
            right=False,
        )

    if "breakout_extension_atr" in btc_edge_df.columns:
        btc_edge_df["btc_edge_breakout_atr_bucket"] = pd.cut(
            btc_edge_df["breakout_extension_atr"],
            bins=[
                -np.inf,
                0,
                0.50,
                1.00,
                1.50,
                2.00,
                np.inf,
            ],
            labels=[
                "< 0",
                "0-0.50",
                "0.50-1.00",
                "1.00-1.50",
                "1.50-2.00",
                ">= 2.00",
            ],
            include_lowest=True,
            right=False,
        )

    if "breakout_volume_ratio" in btc_edge_df.columns:
        btc_edge_df["btc_edge_breakout_volume_bucket"] = pd.cut(
            btc_edge_df["breakout_volume_ratio"],
            bins=[
                -np.inf,
                1,
                1.50,
                2.00,
                3.00,
                np.inf,
            ],
            labels=[
                "< 1.00",
                "1.00-1.50",
                "1.50-2.00",
                "2.00-3.00",
                ">= 3.00",
            ],
            include_lowest=True,
            right=False,
        )

    # =====================================================
    # DATA AVAILABILITY
    # =====================================================

    required_new_columns = [
        "btc_corr_5m_1h",
        "btc_beta_5m_1h",
        "btc_r2_5m_1h",
        "btc_signed_move_15m_pct",
        "btc_signed_move_1h_pct",
        "btc_direction_alignment",
        "btc_trade_alignment",
        "btc_trade_risk_state",
        "btc_relationship_label",
    ]

    missing_new_columns = [
        column
        for column in required_new_columns
        if column not in btc_edge_df.columns
    ]

    if missing_new_columns:
        st.warning(
            "Some new BTC columns are not available yet: "
            + ", ".join(missing_new_columns)
        )

    if "btc_corr_5m_1h" not in btc_edge_df.columns:
        st.info(
            "No fast BTC correlation data available yet."
        )

    else:
        available_df = btc_edge_df.dropna(
            subset=[
                "pnl",
                "btc_corr_5m_1h",
            ]
        ).copy()

        total_filtered_trades = len(btc_edge_df)
        available_trades = len(available_df)

        availability_pct = (
            round(
                available_trades
                / total_filtered_trades
                * 100,
                2,
            )
            if total_filtered_trades
            else 0
        )

        # =================================================
        # CONTROLS
        # =================================================

        control_1, control_2 = st.columns(2)

        with control_1:
            btc_edge_min_trades = st.slider(
                "Minimum trades per group",
                min_value=1,
                max_value=50,
                value=3,
                key="btc_alignment_edge_min_trades",
            )

        with control_2:
            btc_edge_side = st.selectbox(
                "Trade side",
                options=[
                    "ALL",
                    "LONG",
                    "SHORT",
                ],
                index=0,
                key="btc_alignment_edge_side",
            )

        if btc_edge_side != "ALL":
            available_df = available_df[
                available_df["side"]
                == btc_edge_side
            ].copy()

        # =================================================
        # QUICK METRICS
        # =================================================

        pnl_values = pd.to_numeric(
            available_df["pnl"],
            errors="coerce",
        ).dropna()

        total_trades = len(pnl_values)
        wins = int((pnl_values > 0).sum())
        losses = int((pnl_values <= 0).sum())

        winrate = (
            round(
                (pnl_values > 0).mean() * 100,
                2,
            )
            if total_trades
            else 0
        )

        net_pnl = (
            round(pnl_values.sum(), 4)
            if total_trades
            else 0
        )

        overall_pf = btc_edge_profit_factor(
            pnl_values
        )

        avg_corr = (
            round(
                available_df[
                    "btc_corr_5m_1h"
                ].mean(),
                4,
            )
            if not available_df.empty
            else 0
        )

        avg_beta = (
            round(
                available_df[
                    "btc_beta_5m_1h"
                ].mean(),
                4,
            )
            if (
                "btc_beta_5m_1h"
                in available_df.columns
                and not available_df.empty
            )
            else 0
        )

        m1, m2, m3, m4, m5, m6 = st.columns(6)

        m1.metric("Available Trades", total_trades)
        m2.metric("Winrate", f"{winrate}%")
        m3.metric(
            "Profit Factor",
            (
                overall_pf
                if overall_pf is not None
                else "No losses"
            ),
        )
        m4.metric("Net PnL", f"{net_pnl}%")
        m5.metric("Avg Corr 1h", avg_corr)
        m6.metric("Avg Beta 1h", avg_beta)

        st.caption(
            f"Fast BTC data available for "
            f"{available_trades}/{total_filtered_trades} "
            f"filtered trades ({availability_pct}%). "
            f"Current side selection: {btc_edge_side}."
        )

        # =================================================
        # RELATIONSHIP LABEL
        # =================================================

        st.markdown("---")
        st.markdown("### BTC Relationship Performance")

        if "btc_relationship_label" in available_df.columns:
            relationship_report = build_btc_edge_report(
                available_df,
                "btc_relationship_label",
                btc_edge_min_trades,
            )

            if relationship_report.empty:
                st.info(
                    "Not enough trades by BTC relationship."
                )
            else:
                st.dataframe(
                    relationship_report,
                    use_container_width=True,
                    hide_index=True,
                )
        else:
            st.info(
                "Missing column: btc_relationship_label"
            )

        # =================================================
        # TRADE RISK STATE
        # =================================================

        st.markdown("### BTC Risk State Performance")

        if "btc_trade_risk_state" in available_df.columns:
            risk_report = build_btc_edge_report(
                available_df,
                "btc_trade_risk_state",
                btc_edge_min_trades,
            )

            if risk_report.empty:
                st.info(
                    "Not enough trades by BTC risk state."
                )
            else:
                st.dataframe(
                    risk_report,
                    use_container_width=True,
                    hide_index=True,
                )
        else:
            st.info(
                "Missing column: btc_trade_risk_state"
            )

        # =================================================
        # FAST CORRELATION
        # =================================================

        st.markdown("### Fast Correlation Performance")

        corr_report = build_btc_edge_report(
            available_df,
            "btc_corr_5m_1h_bucket",
            btc_edge_min_trades,
        )

        if corr_report.empty:
            st.info(
                "Not enough fast correlation data."
            )
        else:
            st.dataframe(
                corr_report,
                use_container_width=True,
                hide_index=True,
            )

        # =================================================
        # FAST BETA
        # =================================================

        st.markdown("### Fast Beta Performance")

        beta_report = build_btc_edge_report(
            available_df,
            "btc_beta_5m_1h_bucket",
            btc_edge_min_trades,
        )

        if beta_report.empty:
            st.info(
                "Not enough fast beta data."
            )
        else:
            st.dataframe(
                beta_report,
                use_container_width=True,
                hide_index=True,
            )

        # =================================================
        # CORRELATION × DIRECTION MATRIX
        # =================================================

        st.markdown("---")
        st.markdown(
            "### Correlation × BTC Direction Matrix"
        )

        matrix_metric_options = {
            "Profit Factor": "profit_factor",
            "Winrate": "winrate",
            "Net PnL": "net_pnl",
            "Trades": "trades",
        }

        selected_matrix_metric_label = st.selectbox(
            "Matrix metric",
            options=list(
                matrix_metric_options.keys()
            ),
            index=0,
            key="btc_alignment_direction_matrix_metric",
        )

        selected_matrix_metric = (
            matrix_metric_options[
                selected_matrix_metric_label
            ]
        )

        direction_matrix_report = build_btc_edge_report(
            available_df,
            [
                "btc_corr_5m_1h_bucket",
                "btc_direction_alignment",
            ],
            btc_edge_min_trades,
        )

        if direction_matrix_report.empty:
            st.info(
                "Not enough data for correlation × direction."
            )
        else:
            direction_matrix = (
                direction_matrix_report
                .pivot_table(
                    index="btc_corr_5m_1h_bucket",
                    columns="btc_direction_alignment",
                    values=selected_matrix_metric,
                    aggfunc="first",
                )
            )

            st.dataframe(
                direction_matrix,
                use_container_width=True,
            )

        # =================================================
        # CORRELATION × BETA MATRIX
        # =================================================

        st.markdown(
            "### Correlation × Beta Matrix"
        )

        beta_matrix_metric_label = st.selectbox(
            "Correlation × Beta metric",
            options=list(
                matrix_metric_options.keys()
            ),
            index=0,
            key="btc_alignment_beta_matrix_metric",
        )

        beta_matrix_metric = (
            matrix_metric_options[
                beta_matrix_metric_label
            ]
        )

        corr_beta_report = build_btc_edge_report(
            available_df,
            [
                "btc_corr_5m_1h_bucket",
                "btc_beta_5m_1h_bucket",
            ],
            btc_edge_min_trades,
        )

        if corr_beta_report.empty:
            st.info(
                "Not enough data for correlation × beta."
            )
        else:
            corr_beta_matrix = (
                corr_beta_report
                .pivot_table(
                    index="btc_corr_5m_1h_bucket",
                    columns="btc_beta_5m_1h_bucket",
                    values=beta_matrix_metric,
                    aggfunc="first",
                )
            )

            st.dataframe(
                corr_beta_matrix,
                use_container_width=True,
            )

        # =================================================
        # CONFLUENCE EXPLORER
        # =================================================

        st.markdown("---")
        st.markdown("### 🔎 Confluence Explorer")

        st.caption(
            "Cruza BTC con compresión, señales, swings, "
            "volumen y calidad de entrada."
        )

        confluence_dimensions = {
            "Compression Score": "compression_score_bucket",
            "Range Ratio": "range_ratio_bucket",
            "ATR Ratio": "atr_ratio_bucket",
            "Compression Volume": "volume_ratio_bucket",
            "Average Body Ratio": "avg_body_pct_bucket",
            "Compression Range %": "compression_range_pct_bucket",
            
            "BTC Relationship": "btc_relationship_label",
            "BTC Risk State": "btc_trade_risk_state",
            "BTC Trade Alignment": "btc_trade_alignment",
            "BTC Direction Alignment": "btc_direction_alignment",
            "BTC State": "btc_context_state",
            "BTC Direction 15m": "btc_direction_15m",
            "BTC Direction 1h": "btc_direction_1h",
            "Fast Correlation": "btc_corr_5m_1h_bucket",
            "Fast Beta": "btc_beta_5m_1h_bucket",
            "BTC Move 15m": "btc_move_15m_bucket",
            "BTC Move 1h": "btc_move_1h_bucket",

            "Side": "side",
            "Signal Trend": "signal_trend",
            "Signal Direction": "signal_direction",
            "Signal Momentum": "signal_momentum",

            "Compression Quality": "compression_quality_label",
            "Compression Shape": "compression_shape",
            "Compression Score": (
                "btc_edge_compression_score_bucket"
            ),
            "Trend Score": (
                "btc_edge_trend_score_bucket"
            ),
            "Breakout Extension ATR": (
                "btc_edge_breakout_atr_bucket"
            ),
            "Breakout Volume": (
                "btc_edge_breakout_volume_bucket"
            ),

            "Near Swing High 15m": "near_swing_high_15m",
            "Near Swing Low 15m": "near_swing_low_15m",
            "Near Swing High 1h": "near_swing_high_1h",
            "Near Swing Low 1h": "near_swing_low_1h",
            "Near Swing High 4h": "near_swing_high_4h",
            "Near Swing Low 4h": "near_swing_low_4h",

            "Volume Tier": "volume_tier",
            "RVOL 15m Tier": "rvol_tier_15m",
            "RVOL 1h Tier": "rvol_tier_1h",
        }

        confluence_dimensions = {
            label: column
            for label, column
            in confluence_dimensions.items()
            if column in available_df.columns
        }

        if len(confluence_dimensions) < 2:
            st.info(
                "Not enough dimensions available yet."
            )

        else:
            dimension_labels = list(
                confluence_dimensions.keys()
            )

            selector_1, selector_2, selector_3 = (
                st.columns(3)
            )

            with selector_1:
                dimension_a_label = st.selectbox(
                    "Dimension A",
                    options=dimension_labels,
                    index=0,
                    key="btc_edge_dimension_a",
                )

            with selector_2:
                default_b_index = (
                    1
                    if len(dimension_labels) > 1
                    else 0
                )

                dimension_b_label = st.selectbox(
                    "Dimension B",
                    options=dimension_labels,
                    index=default_b_index,
                    key="btc_edge_dimension_b",
                )

            with selector_3:
                dimension_c_options = [
                    "None",
                    *dimension_labels,
                ]

                dimension_c_label = st.selectbox(
                    "Dimension C",
                    options=dimension_c_options,
                    index=0,
                    key="btc_edge_dimension_c",
                )

            selected_labels = [
                dimension_a_label,
                dimension_b_label,
            ]

            if dimension_c_label != "None":
                selected_labels.append(
                    dimension_c_label
                )

            if len(set(selected_labels)) != len(
                selected_labels
            ):
                st.warning(
                    "Choose different dimensions."
                )

            else:
                selected_columns = [
                    confluence_dimensions[label]
                    for label in selected_labels
                ]

                confluence_report = (
                    build_btc_edge_report(
                        available_df,
                        selected_columns,
                        btc_edge_min_trades,
                    )
                )

                if confluence_report.empty:
                    st.info(
                        "No combinations meet the minimum "
                        "trade requirement."
                    )
                else:
                    st.dataframe(
                        confluence_report,
                        use_container_width=True,
                        hide_index=True,
                    )

        # =================================================
        # AUTOMATIC EDGE DISCOVERY
        # =================================================

        st.markdown("---")
        st.markdown("### 🏆 Best BTC Confluences")

        st.caption(
            "Explora automáticamente combinaciones predefinidas. "
            "Un PF alto con pocas operaciones sigue siendo provisional."
        )

        automatic_pairs = [
            (
                "btc_relationship_label",
                "compression_quality_label",
            ),
            (
                "btc_relationship_label",
                "compression_shape",
            ),
            (
                "btc_trade_risk_state",
                "signal_direction",
            ),
            (
                "btc_trade_risk_state",
                "signal_momentum",
            ),
            (
                "btc_corr_5m_1h_bucket",
                "btc_beta_5m_1h_bucket",
            ),
            (
                "btc_corr_5m_1h_bucket",
                "near_swing_high_1h",
            ),
            (
                "btc_corr_5m_1h_bucket",
                "near_swing_high_4h",
            ),
            (
                "btc_relationship_label",
                "btc_edge_breakout_atr_bucket",
            ),
            (
                "btc_relationship_label",
                "btc_edge_breakout_volume_bucket",
            ),
            (
                "btc_direction_alignment",
                "compression_quality_label",
            ),
            (
                "btc_direction_alignment",
                "signal_direction",
            ),
            (
                "btc_move_1h_bucket",
                "signal_momentum",
            ),
        ]

        automatic_reports = []

        for column_a, column_b in automatic_pairs:
            if (
                column_a not in available_df.columns
                or column_b not in available_df.columns
            ):
                continue

            pair_report = build_btc_edge_report(
                available_df,
                [column_a, column_b],
                btc_edge_min_trades,
            )

            if pair_report.empty:
                continue

            pair_report = pair_report.copy()

            pair_report.insert(
                0,
                "analysis",
                f"{column_a} × {column_b}",
            )

            pair_report["sample_strength"] = np.select(
                [
                    pair_report["trades"] >= 30,
                    pair_report["trades"] >= 10,
                ],
                [
                    "STRONGER_SAMPLE",
                    "PROMISING",
                ],
                default="PROVISIONAL",
            )

            pair_report = pair_report.rename(
                columns={
                    column_a: "value_a",
                    column_b: "value_b",
                }
            )

            automatic_reports.append(
                pair_report
            )

        if not automatic_reports:
            st.info(
                "No automatic confluences meet the "
                "minimum trade requirement."
            )

        else:
            best_confluences = pd.concat(
                automatic_reports,
                ignore_index=True,
            )

            best_confluences = (
                best_confluences
                .sort_values(
                    by=[
                        "profit_factor",
                        "net_pnl",
                        "trades",
                        "winrate",
                    ],
                    ascending=[
                        False,
                        False,
                        False,
                        False,
                    ],
                    na_position="last",
                )
            )

            st.dataframe(
                best_confluences.head(50),
                use_container_width=True,
                hide_index=True,
            )

        # =================================================
        # RAW DATA
        # =================================================

        with st.expander(
            "Raw BTC Alignment Trades"
        ):
            raw_columns = [
                "entry_ts_dt",
                "symbol",
                "side",
                "pnl",
                "exit_reason",

                "btc_signed_move_15m_pct",
                "btc_signed_move_1h_pct",
                "btc_direction_alignment",

                "btc_corr_5m_1h",
                "btc_beta_5m_1h",
                "btc_r2_5m_1h",

                "btc_trade_alignment",
                "btc_trade_risk_state",
                "btc_relationship_label",

                "signal_direction",
                "signal_momentum",

                "compression_quality_label",
                "compression_shape",
                "compression_score",
                "trend_score",

                "breakout_extension_atr",
                "breakout_volume_ratio",

                "near_swing_high_15m",
                "near_swing_high_1h",
                "near_swing_high_4h",
            ]

            raw_columns = [
                column
                for column in raw_columns
                if column in available_df.columns
            ]

            raw_alignment_table = (
                available_df[raw_columns]
                .copy()
            )

            st.dataframe(
                raw_alignment_table,
                use_container_width=True,
                hide_index=True,
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
    
    # ==========================
    # COMPRESSION ZONE
    # ==========================

    if (
        compression_high is not None
        and compression_low is not None
        and not pd.isna(compression_high)
        and not pd.isna(compression_low)
    ):
        fig.add_hrect(
            y0=float(compression_low),
            y1=float(compression_high),
            fillcolor="#8b5cf6",
            opacity=0.10,
            line_width=0,
            layer="below",
        )

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
            line_color="#22c55e",
            line_width=1,
        )

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

    st.plotly_chart(
        fig,
        use_container_width=True,
        config={"displayModeBar": False},
    )
    
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
    

    st.plotly_chart(
    fig,
    use_container_width=True,
    config={"displayModeBar": False}
    )

    
def render_pipeline_card(row):
    state = row.get("state", "N/A")
    symbol = row.get("symbol", "N/A")
    color = state_color(state)
    qcolor = score_color(row.get("compression_score", 0))

    quality = row.get("compression_quality_label", "N/A")
    quality_col = quality_color(quality)
    stars = shape_stars(quality)

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
        m1, m2, m3, m4 = st.columns(4)

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
            st.markdown("##### Shape / Quality")
            st.markdown(
                f"""
                <div style="font-size:18px;font-weight:900;color:{quality_col};">
                    {quality}
                </div>
                <div style="font-size:15px;color:#facc15;">
                    {stars}
                </div>
                Shape: <b>{row.get("compression_shape","N/A")}</b><br>
                Height: <b>{fmt(row.get("compression_height_pct"))}%</b><br>
                Duration: <b>{fmt(row.get("compression_duration"))}</b><br>
                Inside: <b>{fmt(row.get("inside_ratio"))}</b><br>
                Touches: <b>H {fmt(row.get("touches_high"))} / L {fmt(row.get("touches_low"))}</b><br>
                Upper: <b>{fmt(row.get("upper_slope"))}</b><br>
                Lower: <b>{fmt(row.get("lower_slope"))}</b><br>
                ΔSlope: <b>{fmt(row.get("slope_difference"))}</b>
                """,
                unsafe_allow_html=True,
            )

        with m3:
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

        with m4:
            st.markdown("##### Breakout / Pullback")
            st.markdown(
                f"""
                Breakout Price: <b>{fmt(row.get("breakout_price"))}</b><br>
                Pullback: <b>{fmt(row.get("pullback_pct"))}</b><br>
                Hold High: <b>{bool_icon(row.get("holds_compression_high"))}</b><br>
                Continuation: <b>{bool_icon(row.get("continuation"))}</b><br>
                Breakout Detected: <b>{bool_icon(row.get("breakout_detected"))}</b><br>
                Breakout Confirmed: <b>{bool_icon(row.get("breakout_confirmed"))}</b><br>
                Pullback Detected: <b>{bool_icon(row.get("pullback_detected"))}</b><br>
                Continuation Detected: <b>{bool_icon(row.get("continuation_detected"))}</b>
                """,
                unsafe_allow_html=True,
            )
        render_mini_chart(row)
        
def event_badge(event):
    colors = {
        "WATCHING_COMPRESSION": "#8b5cf6",
        "WATCH_CREATED": "#0ea5e9",
        "BREAKOUT_DETECTED": "#38bdf8",
        "WAIT_PULLBACK": "#eab308",
        "ENTRY_READY": "#22c55e",
        "EXPIRED": "#ef4444",
        "IDLE": "#64748b",
    }

    color = colors.get(str(event), "#64748b")

    return f"""
    <span style="
        background:{color};
        color:white;
        padding:3px 8px;
        border-radius:999px;
        font-size:11px;
        font-weight:800;
        white-space:nowrap;
    ">
        {event}
    </span>
    """
    
def detail_value(label, value):
    return f"""
    <div style="
        display:flex;
        justify-content:space-between;
        gap:12px;
        padding:5px 0;
        border-bottom:1px solid #1e293b;
        color:#cbd5e1;
        font-size:13px;
    ">
        <span style="color:#94a3b8;">{label}</span>
        <b>{value}</b>
    </div>
    """
    
def render_watch_event_detail(row):
    event = row.get("event", "N/A")
    logged_at = row.get("logged_at", "N/A")

    html = f"""
    <div style="
        border:1px solid #263244;
        border-radius:14px;
        padding:16px;
        background:#0f172a;
        margin-bottom:14px;
    ">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px;">
            <div style="font-size:18px; font-weight:900; color:#ffffff;">
                {event_badge(event)}
            </div>
            <div style="font-size:12px; color:#94a3b8;">
                {logged_at}
            </div>
        </div>

        <div style="display:grid; grid-template-columns:repeat(3, 1fr); gap:18px;">

            <div>
                <div style="font-weight:900; color:#a78bfa; margin-bottom:8px;">Compression</div>
                {detail_value("Score", fmt(row.get("compression_score"), "—"))}
                {detail_value("Trend Score", fmt(row.get("trend_score"), "—"))}
                {detail_value("Watch Age", fmt(row.get("watch_age"), "—"))}
                {detail_value("Candles Waiting", fmt(row.get("candles_waiting"), "—"))}
                {detail_value("High", fmt(row.get("compression_high"), "—"))}
                {detail_value("Low", fmt(row.get("compression_low"), "—"))}
                {detail_value("Range Ratio", fmt(row.get("range_ratio"), "—"))}
                {detail_value("ATR Ratio", fmt(row.get("atr_ratio"), "—"))}
                {detail_value("Volume Ratio", fmt(row.get("volume_ratio"), "—"))}
                {detail_value("Avg Body %", fmt(row.get("avg_body_pct"), "—"))}
            </div>

            <div>
                <div style="font-weight:900; color:#38bdf8; margin-bottom:8px;">Breakout</div>
                {detail_value("Detected", bool_icon(row.get("breakout_detected")))}
                {detail_value("Reason", row.get("breakout_reason", "—"))}
                {detail_value("Price", fmt(row.get("breakout_price"), "—"))}
                {detail_value("Volume Ratio", fmt(row.get("breakout_volume_ratio"), "—"))}
                {detail_value("Extension %", fmt(row.get("breakout_extension_pct"), "—"))}
                {detail_value("Extension ATR", fmt(row.get("breakout_extension_atr"), "—"))}
            </div>

            <div>
                <div style="font-weight:900; color:#22c55e; margin-bottom:8px;">Pullback</div>
                {detail_value("Pullback %", fmt(row.get("pullback_pct"), "—"))}
                {detail_value("Valid", bool_icon(row.get("valid_pullback")))}
                {detail_value("Hold High", bool_icon(row.get("holds_compression_high")))}
                {detail_value("Continuation", bool_icon(row.get("continuation")))}
                {detail_value("Reason", row.get("reason", "—"))}
            </div>

        </div>
    </div>
    """

    st.html(html)
    
def render_watch_history_card(history_df, symbol):
    if history_df.empty:
        st.info(f"No watch history yet for {symbol}.")
        return

    rows_html = ""

    view = history_df.head(8).copy()

    for _, row in view.iterrows():
        event = row.get("event", "N/A")

        rows_html += f"""
        <tr style="border-bottom:1px solid #1e293b;">

            <td style="padding:12px 8px;">{row.get("logged_at", "N/A")}</td>

            <td style="padding:12px 8px;">{event_badge(event)}</td>

            <td style="padding:12px 8px;">{row.get("reason","—")}</td>

            <td style="padding:12px 8px; color:#38bdf8;">
                <b>{fmt(row.get("compression_high"),"—")}</b>
            </td>

            <td style="padding:12px 8px; color:#22c55e;">
                <b>{fmt(row.get("compression_low"),"—")}</b>
            </td>

            <td style="padding:12px 8px;">{fmt(row.get("watch_age"),"—")}</td>

            <td style="padding:12px 8px;">{fmt(row.get("compression_score"),"—")}</td>

            <td style="padding:12px 8px;">{fmt(row.get("trend_score"),"—")}</td>

            <td style="padding:12px 8px;">{fmt(row.get("range_ratio"),"—")}</td>

            <td style="padding:12px 8px;">{fmt(row.get("atr_ratio"),"—")}</td>

            <td style="padding:12px 8px;">{fmt(row.get("volume_ratio"),"—")}</td>

            <td style="padding:12px 8px;">{fmt(row.get("breakout_price"),"N/A")}</td>

            <td style="padding:12px 8px;">{fmt(row.get("pullback_pct"),"N/A")}</td>

            <td style="padding:12px 8px;">
                {bool_icon(row.get("valid_pullback")) if not pd.isna(row.get("valid_pullback")) else "N/A"}
            </td>

        </tr>
        """

    html = f"""
    <div style="
        border:1px solid #263244;
        border-radius:14px;
        padding:16px;
        margin-top:18px;
        margin-bottom:22px;
        background:#0f172a;
        box-shadow:0 8px 24px rgba(0,0,0,0.28);
    ">
        <div style="
            display:flex;
            align-items:center;
            gap:10px;
            margin-bottom:14px;
        ">
            <div style="
                font-size:20px;
                font-weight:900;
                color:#cbd5e1;
            ">
                WATCH HISTORY ({symbol})
            </div>
            <span style="
                background:#6d28d9;
                color:white;
                padding:3px 8px;
                border-radius:999px;
                font-size:11px;
                font-weight:800;
            ">
                {len(history_df)} events
            </span>
        </div>

        <table class="watch-history-table" style="
            width:100%;
            border-collapse:collapse;
            color:#cbd5e1;
            font-size:13px;
        ">
            <thead>
                <tr style="color:#94a3b8; text-align:left;">

                    <th style="padding:14px 8px; border-bottom:1px solid #263244;">Time</th>

                    <th style="padding:14px 8px; border-bottom:1px solid #263244;">Event</th>

                    <th style="padding:14px 8px; border-bottom:1px solid #263244;">Reason</th>

                    <th style="padding:14px 8px; border-bottom:1px solid #263244;">High</th>

                    <th style="padding:14px 8px; border-bottom:1px solid #263244;">Low</th>

                    <th style="padding:14px 8px; border-bottom:1px solid #263244;">Age</th>

                    <th style="padding:14px 8px; border-bottom:1px solid #263244;">Score</th>

                    <th style="padding:14px 8px; border-bottom:1px solid #263244;">Trend</th>

                    <th style="padding:14px 8px; border-bottom:1px solid #263244;">Range</th>

                    <th style="padding:14px 8px; border-bottom:1px solid #263244;">ATR</th>

                    <th style="padding:14px 8px; border-bottom:1px solid #263244;">Vol</th>

                    <th style="padding:14px 8px; border-bottom:1px solid #263244;">Breakout</th>

                    <th style="padding:14px 8px; border-bottom:1px solid #263244;">Pullback</th>

                    <th style="padding:14px 8px; border-bottom:1px solid #263244;">Valid</th>

                </tr>
            </thead>
            <tbody>
                {rows_html}
            </tbody>
        </table>
    </div>
    """

    st.html(html)

    st.markdown("#### Event details")

    for i, (_, row) in enumerate(view.iterrows()):
        with st.expander(f"{row.get('logged_at', 'N/A')} · {row.get('event', 'N/A')}"):
            render_watch_event_detail(row)

    return

with tab_compression_quality:

    st.markdown("---")
    st.subheader("🎯 Compression Entry Quality")

    required_cols = [
        "side",
        "pnl",
        "real_entry",
        "compression_high",
        "compression_low",
        "breakout_price",
        "entry_vs_compression_pct",
        "entry_vs_breakout_pct",
        "breakout_extension_atr",
        "breakout_extension_pct",
        "breakout_volume_ratio",
    ]

    missing_cols = [c for c in required_cols if c not in df_view.columns]

    if missing_cols:
        st.info(f"Missing columns: {missing_cols}")

    else:
        qdf = add_compression_analytics_buckets(df_view)
        
        audit_numeric_cols = [
            "pnl",
            "leverage",

            "signal_price",
            "entry",
            "real_entry",
            "tp",
            "sl",

            "compression_high",
            "compression_low",
            "breakout_price",
            "entry_ready_price",

            "entry_vs_compression_pct",
            "entry_vs_breakout_pct",

            "breakout_extension_atr",
            "breakout_extension_pct",
            "breakout_volume_ratio",

            "max_favorable_pct",
            "max_adverse_pct",
        ]

        for col in audit_numeric_cols:
            if col in qdf.columns:
                qdf[col] = pd.to_numeric(
                    qdf[col],
                    errors="coerce",
                )
                
        if {
            "real_entry",
            "compression_high",
        }.issubset(qdf.columns):
            qdf["real_entry_vs_compression_pct"] = (
                (
                    qdf["real_entry"]
                    - qdf["compression_high"]
                )
                / qdf["compression_high"]
                * 100
            ).round(5)
            
        if {
            "real_entry",
            "breakout_price",
        }.issubset(qdf.columns):
            qdf["real_entry_vs_breakout_pct"] = (
                (
                    qdf["real_entry"]
                    - qdf["breakout_price"]
                )
                / qdf["breakout_price"]
                * 100
            ).round(5)
            
        if {
            "real_entry",
            "entry_ready_price",
        }.issubset(qdf.columns):
            qdf["real_entry_vs_ready_pct"] = (
                (
                    qdf["real_entry"]
                    - qdf["entry_ready_price"]
                )
                / qdf["entry_ready_price"]
                * 100
            ).round(5)
            
        if {
            "signal_price",
            "real_entry",
        }.issubset(qdf.columns):
            qdf["signal_to_real_entry_pct"] = (
                (
                    qdf["real_entry"]
                    - qdf["signal_price"]
                )
                / qdf["signal_price"]
                * 100
            ).round(5)

        # ==========================================
        # RISK-NORMALIZED METRICS
        # ==========================================

        if {
            "real_entry",
            "sl",
        }.issubset(qdf.columns):
            valid_entry = qdf["real_entry"].gt(0)

            qdf["structural_risk_pct"] = np.nan

            qdf.loc[
                valid_entry,
                "structural_risk_pct",
            ] = (
                (
                    qdf.loc[valid_entry, "real_entry"]
                    - qdf.loc[valid_entry, "sl"]
                ).abs()
                / qdf.loc[valid_entry, "real_entry"]
                * 100
            ).round(5)


        if {
            "real_entry",
            "tp",
        }.issubset(qdf.columns):
            valid_entry = qdf["real_entry"].gt(0)

            qdf["planned_reward_pct"] = np.nan

            qdf.loc[
                valid_entry,
                "planned_reward_pct",
            ] = (
                (
                    qdf.loc[valid_entry, "tp"]
                    - qdf.loc[valid_entry, "real_entry"]
                ).abs()
                / qdf.loc[valid_entry, "real_entry"]
                * 100
            ).round(5)


        if {
            "planned_reward_pct",
            "structural_risk_pct",
        }.issubset(qdf.columns):
            valid_risk = qdf["structural_risk_pct"].gt(0)

            qdf["planned_rr"] = np.nan

            qdf.loc[
                valid_risk,
                "planned_rr",
            ] = (
                qdf.loc[
                    valid_risk,
                    "planned_reward_pct",
                ]
                / qdf.loc[
                    valid_risk,
                    "structural_risk_pct",
                ]
            ).round(4)


        if {
            "pnl",
            "leverage",
        }.issubset(qdf.columns):
            safe_leverage = (
                qdf["leverage"]
                .replace(0, np.nan)
                .fillna(1.0)
            )

            qdf["pnl_unleveraged_pct"] = (
                qdf["pnl"]
                / safe_leverage
            ).round(5)


        if {
            "pnl_unleveraged_pct",
            "structural_risk_pct",
        }.issubset(qdf.columns):
            valid_risk = qdf["structural_risk_pct"].gt(0)

            qdf["realized_r"] = np.nan

            qdf.loc[
                valid_risk,
                "realized_r",
            ] = (
                qdf.loc[
                    valid_risk,
                    "pnl_unleveraged_pct",
                ]
                / qdf.loc[
                    valid_risk,
                    "structural_risk_pct",
                ]
            ).round(4)


        if "structural_risk_pct" in qdf.columns:
            qdf["structural_risk_bucket"] = pd.cut(
                qdf["structural_risk_pct"],
                bins=[
                    -float("inf"),
                    2.0,
                    3.0,
                    5.0,
                    10.0,
                    float("inf"),
                ],
                labels=[
                    "<=2%",
                    "2-3%",
                    "3-5%",
                    "5-10%",
                    ">10%",
                ],
                include_lowest=True,
            )
            
        timestamp_cols = [
            "compression_created_ts",
            "signal_ts",
            "breakout_ts",
            "entry_ready_ts",
            "entry_ts",
            "exit_ts",
        ]
        
        def normalize_mixed_timestamp(series):
            numeric = pd.to_numeric(
                series,
                errors="coerce",
            )

            result = pd.Series(
                pd.NaT,
                index=series.index,
                dtype="datetime64[ns, UTC]",
            )

            milliseconds_mask = (
                numeric.notna()
                & numeric.abs().ge(100_000_000_000)
            )

            seconds_mask = (
                numeric.notna()
                & ~milliseconds_mask
            )

            text_mask = numeric.isna()

            if milliseconds_mask.any():
                result.loc[milliseconds_mask] = pd.to_datetime(
                    numeric.loc[milliseconds_mask],
                    unit="ms",
                    errors="coerce",
                    utc=True,
                )

            if seconds_mask.any():
                result.loc[seconds_mask] = pd.to_datetime(
                    numeric.loc[seconds_mask],
                    unit="s",
                    errors="coerce",
                    utc=True,
                )

            if text_mask.any():
                result.loc[text_mask] = pd.to_datetime(
                    series.loc[text_mask],
                    errors="coerce",
                    utc=True,
                )

            return result

        for col in timestamp_cols:
            if col in qdf.columns:
                qdf[f"{col}_dt"] = normalize_mixed_timestamp(
                    qdf[col]
                )
                
        if {
            "compression_created_ts_dt",
            "breakout_ts_dt",
        }.issubset(qdf.columns):
            qdf["watch_to_breakout_minutes"] = (
                (
                    qdf["breakout_ts_dt"]
                    - qdf["compression_created_ts_dt"]
                )
                .dt.total_seconds()
                .div(60)
                .round(2)
            )
            
        if {
            "breakout_ts_dt",
            "entry_ready_ts_dt",
        }.issubset(qdf.columns):
            qdf["breakout_to_ready_minutes"] = (
                (
                    qdf["entry_ready_ts_dt"]
                    - qdf["breakout_ts_dt"]
                )
                .dt.total_seconds()
                .div(60)
                .round(2)
            )
            
        if {
            "entry_ready_ts_dt",
            "entry_ts_dt",
        }.issubset(qdf.columns):
            qdf["ready_to_entry_seconds"] = (
                (
                    qdf["entry_ts_dt"]
                    - qdf["entry_ready_ts_dt"]
                )
                .dt.total_seconds()
                .round(2)
            )

        for col in [
            "pnl",
            "entry_vs_compression_pct",
            "entry_vs_breakout_pct",
            "breakout_extension_atr",
            "breakout_extension_pct",
            "breakout_volume_ratio",
            "max_favorable_pct",
            "max_adverse_pct",
        ]:
            if col in qdf.columns:
                qdf[col] = pd.to_numeric(qdf[col], errors="coerce")

        qdf = qdf.dropna(subset=["pnl", "entry_vs_compression_pct"])

        c1, c2, c3, c4 = st.columns(4)

        c1.metric("Compression trades", len(qdf))
        c2.metric("Avg Entry Distance", f"{qdf['entry_vs_compression_pct'].mean():.3f}%")
        c3.metric("Max Entry Distance", f"{qdf['entry_vs_compression_pct'].max():.3f}%")
        c4.metric("Late Entries > 1%", int((qdf["entry_vs_compression_pct"] > 1.0).sum()))
        
        # =========================
        # BREAKOUT QUALITY REPORTS
        # =========================
        st.markdown("---")
        st.markdown("### 🚀 Breakout Quality")

        st.caption(
            "Analiza individualmente el volumen, la extensión porcentual "
            "del breakout y la distancia entre breakout y entrada."
        )

        breakout_quality_reports = [
            (
                "Breakout Volume Ratio",
                "breakout_volume_ratio_bucket",
            ),
            (
                "Breakout Extension %",
                "breakout_extension_pct_bucket",
            ),
            (
                "Entry vs Breakout %",
                "entry_vs_breakout_bucket",
            ),
        ]

        available_breakout_quality_reports = [
            item
            for item in breakout_quality_reports
            if item[1] in qdf.columns
        ]

        if not available_breakout_quality_reports:
            st.info("No breakout-quality buckets are available.")

        else:
            selected_breakout_report_title = st.selectbox(
                "Breakout analysis",
                options=[
                    title
                    for title, _ in available_breakout_quality_reports
                ],
                key="compression_quality_breakout_analysis",
            )

            selected_breakout_report_col = next(
                column
                for title, column in available_breakout_quality_reports
                if title == selected_breakout_report_title
            )

            breakout_quality_report = build_compression_analytics_report(
                qdf,
                [selected_breakout_report_col],
                min_trades=1,
            )

            if breakout_quality_report.empty:
                st.info(
                    "There are no trades for the selected breakout analysis."
                )

            else:
                st.dataframe(
                    breakout_quality_report,
                    use_container_width=True,
                    hide_index=True,
                )
                
        # =========================
        # BREAKOUT EXTENSION × ENTRY VS BREAKOUT
        # =========================
        st.markdown("---")
        st.markdown(
            "### 🔬 Breakout Extension % × Entry vs Breakout %"
        )

        st.caption(
            "Cruza el tamaño inicial del breakout con la ubicación real "
            "de la entrada respecto del precio de ruptura."
        )

        breakout_combo_cols = [
            "breakout_extension_pct_bucket",
            "entry_vs_breakout_bucket",
        ]

        if not all(col in qdf.columns for col in breakout_combo_cols):
            missing_breakout_combo_cols = [
                col
                for col in breakout_combo_cols
                if col not in qdf.columns
            ]

            st.info(
                "Missing breakout combination columns: "
                f"{missing_breakout_combo_cols}"
            )

        else:
            min_trades_breakout_combo = st.slider(
                "Minimum trades per breakout combination",
                min_value=1,
                max_value=30,
                value=5,
                step=1,
                key="breakout_extension_entry_min_trades",
            )

            breakout_entry_combo_report = (
                build_compression_analytics_report(
                    qdf,
                    breakout_combo_cols,
                    min_trades=min_trades_breakout_combo,
                )
            )

            if breakout_entry_combo_report.empty:
                st.info(
                    "No Breakout Extension × Entry vs Breakout "
                    "combinations meet the minimum trade requirement."
                )

            else:
                st.dataframe(
                    breakout_entry_combo_report,
                    use_container_width=True,
                    hide_index=True,
                )

                st.caption(
                    "La tabla está ordenada por Profit Factor, PnL total, "
                    "win rate y cantidad de trades."
                )
                
                robustness_combo_report = (
                    breakout_entry_combo_report.rename(
                        columns={"profit_factor": "pf"}
                    )
                )

                render_bucket_robustness_explorer(
                    source_df=qdf,
                    summary_df=robustness_combo_report,
                    first_bucket_col="breakout_extension_pct_bucket",
                    second_bucket_col="entry_vs_breakout_bucket",
                    first_bucket_label="Breakout Extension %",
                    second_bucket_label="Entry vs Breakout %",
                    key_prefix="breakout_extension_entry_breakout",
                )

        st.markdown("---")

        st.markdown("### Entry Distance from Compression")

        distance_bins = [-999, 0, 0.25, 0.50, 0.75, 1.00, 1.50, 2.00, 999]
        distance_labels = [
            "< 0%",
            "0% - 0.25%",
            "0.25% - 0.50%",
            "0.50% - 0.75%",
            "0.75% - 1.00%",
            "1.00% - 1.50%",
            "1.50% - 2.00%",
            "> 2.00%",
        ]

        qdf["entry_distance_bucket"] = pd.cut(
            qdf["entry_vs_compression_pct"],
            bins=distance_bins,
            labels=distance_labels,
            include_lowest=True,
        )

        entry_distance_summary = (
            qdf
            .groupby("entry_distance_bucket", observed=False)
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

        for col in ["avg_pnl", "total_pnl", "avg_mfe", "avg_mae"]:
            if col in entry_distance_summary.columns:
                entry_distance_summary[col] = entry_distance_summary[col].round(4)

        st.dataframe(entry_distance_summary, use_container_width=True)
        
        st.markdown(
            "### Entry vs Compression High × Breakout ATR"
        )

        qdf["entry_vs_compression_simple"] = pd.cut(
            qdf["entry_vs_compression_pct"],
            bins=[-float("inf"), 0.5, 1.0, 1.5, 2.0, float("inf")],
            labels=["<0.5%", "0.5-1%", "1-1.5%", "1.5-2%", ">2%"],
            include_lowest=True,
        )

        qdf["breakout_atr_simple"] = pd.cut(
            qdf["breakout_extension_atr"],
            bins=[-999, 0.5, 1.0, 1.5, 2.0, 3.0, 999],
            labels=["<0.5 ATR", "0.5-1 ATR", "1-1.5 ATR", "1.5-2 ATR", "2-3 ATR", ">3 ATR"],
            include_lowest=True,
        )

        combo_atr_df = (
            qdf
            .dropna(
                subset=[
                    "entry_vs_compression_simple",
                    "breakout_atr_simple",
                ]
            )
            .groupby(
                [
                    "entry_vs_compression_simple",
                    "breakout_atr_simple",
                ],
                observed=False,
            )
            .agg(
                trades=("pnl", "count"),
                wins=("pnl", lambda x: int((x > 0).sum())),
                losses=("pnl", lambda x: int((x <= 0).sum())),
                winrate=("pnl", lambda x: round((x > 0).mean() * 100, 2)),
                avg_pnl=("pnl", "mean"),
                total_pnl=("pnl", "sum"),
                pf=("pnl", profit_factor),
            )
            .reset_index()
        )

        for col in ["avg_pnl", "total_pnl"]:
            combo_atr_df[col] = combo_atr_df[col].round(4)

        combo_atr_df = combo_atr_df[combo_atr_df["trades"] > 0]

        st.dataframe(
            combo_atr_df.sort_values(
                ["pf", "total_pnl"],
                ascending=False,
                na_position="last"
            ),
            use_container_width=True
        )
        
        render_bucket_robustness_explorer(
            source_df=qdf,
            summary_df=combo_atr_df,
            first_bucket_col="entry_vs_compression_simple",
            second_bucket_col="breakout_atr_simple",
            first_bucket_label="Entry vs Compression High",
            second_bucket_label="Breakout ATR",
            key_prefix="entry_vs_compression_breakout_atr",
        )
        
        st.markdown(
            "### Entry vs Compression High × Breakout Volume"
        )

        qdf["breakout_volume_bucket"] = pd.cut(
            qdf["breakout_volume_ratio"],
            bins=[-999, 1.5, 2.0, 3.0, 5.0, 999],
            labels=["<1.5x", "1.5-2x", "2-3x", "3-5x", ">5x"],
            include_lowest=True,
        )

        combo_volume_df = (
            qdf
            .dropna(
                subset=[
                    "entry_vs_compression_simple",
                    "breakout_volume_bucket",
                ]
            )
            .groupby(
                [
                    "entry_vs_compression_simple",
                    "breakout_volume_bucket",
                ],
                observed=False,
            )
            .agg(
                trades=("pnl", "count"),
                wins=("pnl", lambda x: int((x > 0).sum())),
                losses=("pnl", lambda x: int((x <= 0).sum())),
                winrate=(
                    "pnl",
                    lambda x: round(
                        (x > 0).mean() * 100,
                        2,
                    ),
                ),
                avg_pnl=("pnl", "mean"),
                total_pnl=("pnl", "sum"),
                pf=("pnl", profit_factor),
            )
            .reset_index()
        )

        for col in ["avg_pnl", "total_pnl"]:
            combo_volume_df[col] = combo_volume_df[col].round(4)

        combo_volume_df = combo_volume_df[combo_volume_df["trades"] > 0]

        st.dataframe(
            combo_volume_df.sort_values(
                ["pf", "total_pnl"],
                ascending=False,
                na_position="last"
            ),
            use_container_width=True
        )
        
        render_bucket_robustness_explorer(
            source_df=qdf,
            summary_df=combo_volume_df,
            first_bucket_col="entry_vs_compression_simple",
            second_bucket_col="breakout_volume_bucket",
            first_bucket_label="Entry vs Compression High",
            second_bucket_label="Breakout Volume",
            key_prefix="entry_vs_compression_breakout_volume",
        )
        
        # ==========================================
        # BREAKOUT VOLUME × BREAKOUT EXTENSION ATR
        # ==========================================

        st.markdown("---")
        st.markdown(
            "### Breakout Volume × Breakout Extension ATR"
        )

        st.caption(
            "Cruza el volumen relativo del breakout con su "
            "desplazamiento en ATR e incorpora resultados "
            "normalizados por riesgo."
        )
        
        entry_bucket_order = [
            "<0.5%",
            "0.5-1%",
            "1-1.5%",
            "1.5-2%",
            ">2%",
        ]

        available_entry_bucket_values = set(
            qdf["entry_vs_compression_simple"]
            .dropna()
            .astype(str)
            .unique()
        )

        available_entry_buckets = [
            bucket
            for bucket in entry_bucket_order
            if bucket in available_entry_bucket_values
        ]

        default_entry_buckets = (
            [">2%"]
            if ">2%" in available_entry_buckets
            else []
        )

        selected_entry_buckets = st.multiselect(
            "Filter by Entry vs Compression High",
            options=available_entry_buckets,
            default=default_entry_buckets,
            key="volume_atr_entry_compression_filter",
        )

        qdf["breakout_atr_validation_bucket"] = pd.cut(
            qdf["breakout_extension_atr"],
            bins=[
                -float("inf"),
                1.0,
                2.0,
                3.0,
                float("inf"),
            ],
            labels=[
                "<1 ATR",
                "1-2 ATR",
                "2-3 ATR",
                ">3 ATR",
            ],
            include_lowest=True,
        )

        volume_atr_base = qdf.copy()

        if selected_entry_buckets:
            volume_atr_base = volume_atr_base[
                volume_atr_base[
                    "entry_vs_compression_simple"
                ]
                .astype(str)
                .isin(selected_entry_buckets)
            ].copy()

        volume_atr_source = volume_atr_base.dropna(
            subset=[
                "breakout_volume_bucket",
                "breakout_atr_validation_bucket",
                "pnl",
            ]
        ).copy()
        
        st.caption(
            f"Filtered trades: {len(volume_atr_source)}"
        )

        volume_atr_agg = {
            "trades": (
                "pnl",
                "count",
            ),
            "wins": (
                "pnl",
                lambda x: int((x > 0).sum()),
            ),
            "losses": (
                "pnl",
                lambda x: int((x <= 0).sum()),
            ),
            "winrate": (
                "pnl",
                lambda x: round(
                    (x > 0).mean() * 100,
                    2,
                ),
            ),
            "avg_pnl": (
                "pnl",
                "mean",
            ),
            "median_pnl": (
                "pnl",
                "median",
            ),
            "total_pnl": (
                "pnl",
                "sum",
            ),
            "pf": (
                "pnl",
                profit_factor,
            ),
            # Valor real dentro del bucket de volumen
            "avg_breakout_volume": (
                "breakout_volume_ratio",
                "mean",
            ),
            "median_breakout_volume": (
                "breakout_volume_ratio",
                "median",
            ),

            # Valor real dentro del bucket de extensión ATR
            "avg_breakout_extension_atr": (
                "breakout_extension_atr",
                "mean",
            ),
            "median_breakout_extension_atr": (
                "breakout_extension_atr",
                "median",
            ),

            # Distancia real desde compression high hasta entry
            "avg_entry_vs_compression_pct": (
                "entry_vs_compression_pct",
                "mean",
            ),
            "median_entry_vs_compression_pct": (
                "entry_vs_compression_pct",
                "median",
            ),
        }

        if "realized_r" in volume_atr_source.columns:
            volume_atr_agg.update({
                "net_r": (
                    "realized_r",
                    "sum",
                ),
                "avg_r": (
                    "realized_r",
                    "mean",
                ),
                "median_r": (
                    "realized_r",
                    "median",
                ),
                "pf_r": (
                    "realized_r",
                    profit_factor,
                ),
            })

        volume_atr_report = (
            volume_atr_source
            .groupby(
                [
                    "breakout_volume_bucket",
                    "breakout_atr_validation_bucket",
                ],
                observed=False,
            )
            .agg(**volume_atr_agg)
            .reset_index()
        )

        volume_atr_report = volume_atr_report[
            volume_atr_report["trades"] > 0
        ].copy()

        round_cols = [
            "avg_pnl",
            "median_pnl",
            "total_pnl",
            "pf",
            "net_r",
            "avg_r",
            "median_r",
            "pf_r",

            # Valores reales de los filtros
            "avg_breakout_volume",
            "median_breakout_volume",
            "avg_breakout_extension_atr",
            "median_breakout_extension_atr",
            "avg_entry_vs_compression_pct",
            "median_entry_vs_compression_pct",
        ]

        for col in round_cols:
            if col in volume_atr_report.columns:
                volume_atr_report[col] = (
                    volume_atr_report[col].round(4)
                )

        sort_metric = (
            "pf_r"
            if "pf_r" in volume_atr_report.columns
            else "pf"
        )

        st.dataframe(
            volume_atr_report.sort_values(
                [
                    sort_metric,
                    "trades",
                ],
                ascending=[
                    False,
                    False,
                ],
                na_position="last",
            ),
            use_container_width=True,
            hide_index=True,
                        column_config={
                "avg_breakout_volume": st.column_config.NumberColumn(
                    "Avg Volume",
                    format="%.2fx",
                ),
                "median_breakout_volume": st.column_config.NumberColumn(
                    "Median Volume",
                    format="%.2fx",
                ),
                "avg_breakout_extension_atr": st.column_config.NumberColumn(
                    "Avg Extension ATR",
                    format="%.2f ATR",
                ),
                "median_breakout_extension_atr": st.column_config.NumberColumn(
                    "Median Extension ATR",
                    format="%.2f ATR",
                ),
                "avg_entry_vs_compression_pct": st.column_config.NumberColumn(
                    "Avg Entry vs High",
                    format="%.2f%%",
                ),
                "median_entry_vs_compression_pct": st.column_config.NumberColumn(
                    "Median Entry vs High",
                    format="%.2f%%",
                ),
            },
        )
        
        render_bucket_robustness_explorer(
            source_df=volume_atr_source,
            summary_df=volume_atr_report,
            first_bucket_col="breakout_volume_bucket",
            second_bucket_col="breakout_atr_validation_bucket",
            first_bucket_label="Breakout Volume",
            second_bucket_label="Breakout Extension ATR",
            key_prefix="breakout_volume_extension_atr",
        )

        st.markdown("### SL Late Entry Cases")

        sl_late_df = qdf.copy()

        if "exit_reason" in sl_late_df.columns:
            sl_late_df = sl_late_df[
                sl_late_df["exit_reason"].astype(str).str.upper() == "SL"
            ]

        sl_cols = [
            "symbol",
            "side",
            "entry_ts_dt",
            "pnl",
            "real_entry",
            "compression_high",
            "compression_low",
            "breakout_price",
            "entry_vs_compression_pct",
            "entry_vs_breakout_pct",
            "breakout_extension_atr",
            "breakout_extension_pct",
            "breakout_volume_ratio",
            "compression_score",
            "trend_score",
            "max_favorable_pct",
            "max_adverse_pct",
        ]

        existing_sl_cols = [c for c in sl_cols if c in sl_late_df.columns]

        st.dataframe(
            sl_late_df[existing_sl_cols]
            .sort_values("entry_vs_compression_pct", ascending=False)
            .head(50),
            use_container_width=True
        )

        st.markdown("### Breakout Extension ATR")

        atr_bins = [-999, 0.25, 0.50, 1.00, 1.50, 2.00, 3.00, 999]
        atr_labels = [
            "< 0.25 ATR",
            "0.25 - 0.50 ATR",
            "0.50 - 1.00 ATR",
            "1.00 - 1.50 ATR",
            "1.50 - 2.00 ATR",
            "2.00 - 3.00 ATR",
            "> 3.00 ATR",
        ]

        qdf["breakout_atr_bucket"] = pd.cut(
            qdf["breakout_extension_atr"],
            bins=atr_bins,
            labels=atr_labels,
            include_lowest=True,
        )

        breakout_atr_summary = (
            qdf
            .dropna(subset=["breakout_atr_bucket"])
            .groupby("breakout_atr_bucket", observed=False)
            .agg(
                trades=("pnl", "count"),
                wins=("pnl", lambda x: int((x > 0).sum())),
                losses=("pnl", lambda x: int((x <= 0).sum())),
                winrate=("pnl", lambda x: round((x > 0).mean() * 100, 2)),
                avg_pnl=("pnl", "mean"),
                total_pnl=("pnl", "sum"),
                avg_entry_distance=("entry_vs_compression_pct", "mean"),
                pf=("pnl", profit_factor),
            )
            .reset_index()
        )

        for col in ["avg_pnl", "total_pnl", "avg_entry_distance"]:
            breakout_atr_summary[col] = breakout_atr_summary[col].round(4)

        st.dataframe(breakout_atr_summary, use_container_width=True)

        st.markdown("### Late Entry Cases")

        late_cols = [
            "symbol",
            "side",
            "entry_ts_dt",
            "exit_reason",
            "pnl",
            "real_entry",
            "compression_high",
            "compression_low",
            "breakout_price",
            "entry_vs_compression_pct",
            "entry_vs_breakout_pct",
            "breakout_extension_atr",
            "breakout_extension_pct",
            "breakout_volume_ratio",
            "compression_score",
            "trend_score",
            "max_favorable_pct",
            "max_adverse_pct",
        ]

        existing_late_cols = [c for c in late_cols if c in qdf.columns]

        late_df = (
            qdf[existing_late_cols]
            .sort_values("entry_vs_compression_pct", ascending=False)
        )

        st.dataframe(
            late_df.head(50),
            use_container_width=True
        )

        st.markdown("### Entry Distance vs PnL")

        scatter_df = qdf.dropna(subset=["entry_vs_compression_pct", "pnl"]).copy()

        if not scatter_df.empty:
            fig = go.Figure()

            fig.add_trace(
                go.Scatter(
                    x=scatter_df["entry_vs_compression_pct"],
                    y=scatter_df["pnl"],
                    mode="markers",
                    text=scatter_df["symbol"] if "symbol" in scatter_df.columns else None,
                    customdata=scatter_df[["side", "exit_reason"]] if all(c in scatter_df.columns for c in ["side", "exit_reason"]) else None,
                    hovertemplate=(
                        "Entry distance: %{x:.3f}%<br>"
                        "PnL: %{y:.3f}%<br>"
                        "Symbol: %{text}<br>"
                        "<extra></extra>"
                    ),
                )
            )

            fig.update_layout(
                xaxis_title="Entry distance from compression (%)",
                yaxis_title="PnL (%)",
                height=450,
            )

            st.plotly_chart(fig, use_container_width=True)
            
with tab_compression_analytics:

    st.markdown("---")
    st.subheader("🔬 Compression Analytics")

    st.caption(
        "Busca edge estadístico en la estructura de las compresiones. "
        "Todos los resultados respetan el filtro global de fechas."
    )

    required_cols = ["pnl"]

    missing_cols = [
        col for col in required_cols
        if col not in df_view.columns
    ]

    if missing_cols:
        st.info(f"Missing columns: {missing_cols}")

    else:
        compression_df = add_compression_analytics_buckets(df_view)

        compression_df["pnl"] = pd.to_numeric(
            compression_df["pnl"],
            errors="coerce",
        )

        compression_df = compression_df.dropna(subset=["pnl"]).copy()

        # Keep only rows that have at least one compression field.
        compression_identity_cols = [
            "compression_shape",
            "compression_quality_label",
            "compression_height_pct",
            "compression_duration",
            "inside_ratio",
            "touches_high",
            "touches_low",
        ]

        available_identity_cols = [
            col for col in compression_identity_cols
            if col in compression_df.columns
        ]

        if available_identity_cols:
            compression_df = compression_df[
                compression_df[available_identity_cols]
                .notna()
                .any(axis=1)
            ].copy()

        if compression_df.empty:
            st.info(
                "There are no trades with compression analytics data "
                "inside the selected date range."
            )

        else:
            # =========================
            # FILTERS
            # =========================
            c1, c2, c3 = st.columns(3)

            with c1:
                max_min_trades = max(
                    1,
                    min(50, len(compression_df)),
                )

                min_trades_compression = st.slider(
                    "Minimum trades per group",
                    min_value=1,
                    max_value=max_min_trades,
                    value=min(5, max_min_trades),
                    step=1,
                    key="compression_analytics_min_trades",
                )

            with c2:
                if "side" in compression_df.columns:
                    available_sides = sorted(
                        compression_df["side"]
                        .dropna()
                        .astype(str)
                        .str.upper()
                        .unique()
                        .tolist()
                    )

                    selected_side = st.selectbox(
                        "Side",
                        options=["ALL"] + available_sides,
                        key="compression_analytics_side",
                    )
                else:
                    selected_side = "ALL"

            with c3:
                if "symbol" in compression_df.columns:
                    available_symbols = sorted(
                        compression_df["symbol"]
                        .dropna()
                        .astype(str)
                        .unique()
                        .tolist()
                    )

                    selected_symbol = st.selectbox(
                        "Symbol",
                        options=["ALL"] + available_symbols,
                        key="compression_analytics_symbol",
                    )
                else:
                    selected_symbol = "ALL"

            analytics_df = compression_df.copy()

            if selected_side != "ALL":
                analytics_df = analytics_df[
                    analytics_df["side"]
                    .astype(str)
                    .str.upper()
                    .eq(selected_side)
                ].copy()

            if selected_symbol != "ALL":
                analytics_df = analytics_df[
                    analytics_df["symbol"].astype(str).eq(selected_symbol)
                ].copy()
                
            # =========================
            # DATA COVERAGE
            # =========================
            st.markdown("---")
            st.markdown("### 🧪 Compression Data Coverage")

            st.caption(
                "Muestra qué métricas de compresión tienen datos suficientes, "
                "cuántos valores diferentes poseen y cuáles son constantes."
            )

            compression_analysis_cols = [
                # =========================
                # COMPRESSION PIPELINE
                # =========================
                "compression_state",
                "compression_reason",
                "compression_created_ts",
                "compression_updated_ts",
                "compression_candles_waiting",

                # =========================
                # TREND
                # =========================
                "trend_score",

                # =========================
                # COMPRESSION DETECTOR
                # =========================
                "compression_score",
                "range_ratio",
                "atr_ratio",
                "volume_ratio",
                "avg_body_pct",

                # =========================
                # COMPRESSION STRUCTURE
                # =========================
                "compression_high",
                "compression_low",
                "compression_range_pct",
                "compression_height_pct",
                "compression_duration",
                "upper_slope",
                "lower_slope",
                "slope_difference",
                "touches_high",
                "touches_low",
                "inside_ratio",
                "compression_shape",
                "compression_quality_label",

                # =========================
                # BREAKOUT
                # =========================
                "breakout_ts",
                "breakout_price",
                "breakout_high",
                "breakout_volume_ratio",
                "breakout_extension_pct",
                "breakout_extension_atr",

                # =========================
                # ENTRY
                # =========================
                "entry_ready_price",

                # =========================
                # BTC VELOCITY CONTEXT
                # =========================
                "btc_velocity_15m",
                "btc_velocity_1h",
                "btc_direction_15m",
                "btc_direction_1h",
                "btc_context_state",
                "btc_context_reason",

                # =========================
                # ORIGINAL BTC CORRELATION
                # =========================
                "btc_corr_15m",
                "btc_beta_15m",
                "btc_r2_15m",
                "symbol_move_15m_pct",
                "btc_move_15m_pct",
                "btc_expected_move_15m_pct",
                "btc_residual_move_15m_pct",

                "btc_corr_1h",
                "btc_beta_1h",
                "btc_r2_1h",
                "symbol_move_1h_pct",
                "btc_move_1h_pct",
                "btc_expected_move_1h_pct",
                "btc_residual_move_1h_pct",

                "btc_corr_4h",
                "btc_beta_4h",
                "btc_r2_4h",
                "symbol_move_4h_pct",
                "btc_move_4h_pct",
                "btc_expected_move_4h_pct",
                "btc_residual_move_4h_pct",

                # =========================
                # FAST BTC CORRELATION
                # 5m candles / 1h window
                # =========================
                "btc_corr_5m_1h",
                "btc_beta_5m_1h",
                "btc_r2_5m_1h",
                "btc_corr_available_5m_1h",
                "btc_corr_reason_5m_1h",
                "btc_corr_samples_5m_1h",

                # 5m candles / 4h window
                "btc_corr_5m_4h",
                "btc_beta_5m_4h",
                "btc_r2_5m_4h",
                "btc_corr_available_5m_4h",
                "btc_corr_reason_5m_4h",
                "btc_corr_samples_5m_4h",

                # 5m candles / 24h window
                "btc_corr_5m_24h",
                "btc_beta_5m_24h",
                "btc_r2_5m_24h",
                "btc_corr_available_5m_24h",
                "btc_corr_reason_5m_24h",
                "btc_corr_samples_5m_24h",

                # =========================
                # BTC ALIGNMENT
                # =========================
                "btc_signed_move_15m_pct",
                "btc_signed_move_1h_pct",
                "btc_direction_alignment",
                "btc_trade_alignment",
                "btc_trade_risk_state",
                "btc_relationship_label",

                # =========================
                # OUTCOMES
                # =========================
                "pnl",
                "mfe",
                "mae",
                "max_favorable_pct",
                "max_adverse_pct",
                "exit_reason",
            ]

            coverage_rows = []

            total_analytics_rows = len(analytics_df)

            for column in compression_analysis_cols:
                if column not in analytics_df.columns:
                    coverage_rows.append({
                        "column": column,
                        "status": "MISSING_COLUMN",
                        "non_null": 0,
                        "coverage_pct": 0.0,
                        "unique_values": 0,
                        "constant": False,
                    })
                    continue

                series = analytics_df[column]
                non_null_count = int(series.notna().sum())
                unique_count = int(series.dropna().nunique())

                coverage_pct = (
                    non_null_count / total_analytics_rows * 100
                    if total_analytics_rows > 0
                    else 0
                )

                if non_null_count == 0:
                    status = "EMPTY"
                elif unique_count <= 1:
                    status = "CONSTANT"
                elif coverage_pct < 50:
                    status = "LOW_COVERAGE"
                else:
                    status = "OK"

                coverage_rows.append({
                    "column": column,
                    "status": status,
                    "non_null": non_null_count,
                    "coverage_pct": round(coverage_pct, 2),
                    "unique_values": unique_count,
                    "constant": unique_count == 1,
                })

            coverage_df = pd.DataFrame(coverage_rows)

            coverage_summary = coverage_df["status"].value_counts()

            dc1, dc2, dc3, dc4 = st.columns(4)

            dc1.metric(
                "Available",
                int((coverage_df["status"] != "MISSING_COLUMN").sum()),
            )

            dc2.metric(
                "Missing",
                int((coverage_df["status"] == "MISSING_COLUMN").sum()),
            )

            dc3.metric(
                "Empty",
                int((coverage_df["status"] == "EMPTY").sum()),
            )

            dc4.metric(
                "Constant",
                int((coverage_df["status"] == "CONSTANT").sum()),
            )

            coverage_status_filter = st.multiselect(
                "Coverage status",
                options=[
                    "OK",
                    "LOW_COVERAGE",
                    "CONSTANT",
                    "EMPTY",
                    "MISSING_COLUMN",
                ],
                default=[
                    "LOW_COVERAGE",
                    "CONSTANT",
                    "EMPTY",
                    "MISSING_COLUMN",
                ],
                key="compression_coverage_status_filter",
            )

            filtered_coverage_df = coverage_df[
                coverage_df["status"].isin(coverage_status_filter)
            ].copy()

            st.dataframe(
                filtered_coverage_df.sort_values(
                    ["status", "coverage_pct", "column"],
                    ascending=[True, True, True],
                ),
                use_container_width=True,
                hide_index=True,
            )

            # =========================
            # OVERALL METRICS
            # =========================
            overall_pnl = analytics_df["pnl"].dropna()

            total_trades = len(overall_pnl)
            total_wins = int((overall_pnl > 0).sum())
            total_losses = int((overall_pnl <= 0).sum())

            overall_winrate = (
                round((overall_pnl > 0).mean() * 100, 2)
                if total_trades
                else 0
            )

            overall_pf = compression_profit_factor(overall_pnl)

            overall_avg_pnl = (
                round(overall_pnl.mean(), 4)
                if total_trades
                else 0
            )

            overall_total_pnl = (
                round(overall_pnl.sum(), 4)
                if total_trades
                else 0
            )

            m1, m2, m3, m4, m5, m6 = st.columns(6)

            m1.metric("Compression Trades", total_trades)
            m2.metric("Wins", total_wins)
            m3.metric("Losses", total_losses)
            m4.metric("Winrate", f"{overall_winrate}%")
            m5.metric(
                "Profit Factor",
                overall_pf if overall_pf is not None else "No losses",
            )
            m6.metric(
                "Avg / Total PnL",
                f"{overall_avg_pnl}% / {overall_total_pnl}%",
            )
            
            # =========================
            # DETECTOR COMPONENTS
            # =========================
            st.markdown("---")
            st.markdown("### 🎛️ Detector Components")

            st.caption(
                "Analiza por separado las condiciones que forman el "
                "compression score actual."
            )

            detector_reports = [
                (
                    "Compression Score",
                    "compression_score_bucket",
                ),
                (
                    "Range Ratio",
                    "range_ratio_bucket",
                ),
                (
                    "ATR Ratio",
                    "atr_ratio_bucket",
                ),
                (
                    "Volume Ratio",
                    "volume_ratio_bucket",
                ),
                (
                    "Average Body Ratio",
                    "avg_body_pct_bucket",
                ),
                (
                    "Compression Range %",
                    "compression_range_pct_bucket",
                ),
            ]

            available_detector_reports = [
                item
                for item in detector_reports
                if item[1] in analytics_df.columns
            ]

            if not available_detector_reports:
                st.info(
                    "No detector-component bucket columns are available."
                )

            else:
                selected_detector_title = st.selectbox(
                    "Detector component",
                    options=[
                        title
                        for title, _ in available_detector_reports
                    ],
                    key="compression_detector_component",
                )

                selected_detector_col = next(
                    column
                    for title, column in available_detector_reports
                    if title == selected_detector_title
                )

                detector_report = build_compression_analytics_report(
                    analytics_df,
                    [selected_detector_col],
                    min_trades=min_trades_compression,
                )

                if detector_report.empty:
                    st.info(
                        "Not enough trades for the selected detector component."
                    )

                else:
                    st.dataframe(
                        detector_report,
                        use_container_width=True,
                        hide_index=True,
                    )

            # =========================
            # QUALITY
            # =========================
            st.markdown("### Compression Quality")

            if "compression_quality_label" in analytics_df.columns:
                quality_report = build_compression_analytics_report(
                    analytics_df,
                    ["compression_quality_label"],
                    min_trades=min_trades_compression,
                )

                if quality_report.empty:
                    st.info(
                        "Not enough trades per compression quality group."
                    )
                else:
                    st.dataframe(
                        quality_report,
                        use_container_width=True,
                        hide_index=True,
                    )
            else:
                st.info("Missing column: compression_quality_label")

            # =========================
            # SHAPE
            # =========================
            st.markdown("### Compression Shape")

            if "compression_shape" in analytics_df.columns:
                shape_report = build_compression_analytics_report(
                    analytics_df,
                    ["compression_shape"],
                    min_trades=min_trades_compression,
                )

                if shape_report.empty:
                    st.info(
                        "Not enough trades per compression shape."
                    )
                else:
                    st.dataframe(
                        shape_report,
                        use_container_width=True,
                        hide_index=True,
                    )
            else:
                st.info("Missing column: compression_shape")

            # =========================
            # QUALITY × SHAPE
            # =========================
            st.markdown("### Quality × Shape")

            if all(
                col in analytics_df.columns
                for col in [
                    "compression_quality_label",
                    "compression_shape",
                ]
            ):
                quality_shape_report = build_compression_analytics_report(
                    analytics_df,
                    [
                        "compression_quality_label",
                        "compression_shape",
                    ],
                    min_trades=min_trades_compression,
                )

                if quality_shape_report.empty:
                    st.info(
                        "Not enough trades for Quality × Shape combinations."
                    )
                else:
                    st.dataframe(
                        quality_shape_report,
                        use_container_width=True,
                        hide_index=True,
                    )

            # =========================
            # STRUCTURE BUCKET REPORTS
            # =========================
            st.markdown("---")
            st.markdown("### Structural Variables")

            structural_reports = [
                (
                    "Compression Duration",
                    "compression_duration_bucket",
                ),
                (
                    "Compression Height",
                    "compression_height_bucket",
                ),
                (
                    "Inside Ratio",
                    "inside_ratio_bucket",
                ),
                (
                    "Total Touches",
                    "total_touches_bucket",
                ),
                (
                    "Touch Balance",
                    "touch_balance_bucket",
                ),
                (
                    "Upper Slope Magnitude",
                    "upper_slope_bucket",
                ),
                (
                    "Lower Slope Magnitude",
                    "lower_slope_bucket",
                ),
                (
                    "Slope Difference",
                    "slope_difference_bucket",
                ),
            ]

            available_structural_reports = [
                item
                for item in structural_reports
                if item[1] in analytics_df.columns
            ]

            if not available_structural_reports:
                st.info(
                    "No structural compression bucket columns available."
                )

            else:
                selected_structural_title = st.selectbox(
                    "Structural analysis",
                    options=[
                        title
                        for title, _ in available_structural_reports
                    ],
                    key="compression_structural_analysis",
                )

                selected_structural_col = next(
                    col
                    for title, col in available_structural_reports
                    if title == selected_structural_title
                )

                structural_report = build_compression_analytics_report(
                    analytics_df,
                    [selected_structural_col],
                    min_trades=min_trades_compression,
                )

                if structural_report.empty:
                    st.info(
                        "Not enough trades for the selected structural analysis."
                    )
                else:
                    st.dataframe(
                        structural_report,
                        use_container_width=True,
                        hide_index=True,
                    )

            # =========================
            # ENTRY LOCATION REPORTS
            # =========================
            st.markdown("---")
            st.markdown("### Entry Location")

            entry_reports = [
                (
                    "Entry Distance",
                    "entry_distance_bucket_analytics",
                ),
                (
                    "Entry vs Compression",
                    "entry_vs_compression_bucket",
                ),
                (
                    "Entry vs Breakout",
                    "entry_vs_breakout_bucket",
                ),
                (
                    "Late Entry",
                    "late_entry_label",
                ),
            ]

            available_entry_reports = [
                item
                for item in entry_reports
                if item[1] in analytics_df.columns
            ]

            if not available_entry_reports:
                st.info("No entry-location columns available.")

            else:
                selected_entry_title = st.selectbox(
                    "Entry analysis",
                    options=[
                        title
                        for title, _ in available_entry_reports
                    ],
                    key="compression_entry_analysis",
                )

                selected_entry_col = next(
                    col
                    for title, col in available_entry_reports
                    if title == selected_entry_title
                )

                entry_report = build_compression_analytics_report(
                    analytics_df,
                    [selected_entry_col],
                    min_trades=min_trades_compression,
                )

                if entry_report.empty:
                    st.info(
                        "Not enough trades for the selected entry analysis."
                    )
                else:
                    st.dataframe(
                        entry_report,
                        use_container_width=True,
                        hide_index=True,
                    )

            # =========================
            # SIDE COMPARISON
            # =========================
            if (
                selected_side == "ALL"
                and "side" in analytics_df.columns
            ):
                st.markdown("---")
                st.markdown("### Long vs Short")

                side_report = build_compression_analytics_report(
                    analytics_df,
                    ["side"],
                    min_trades=min_trades_compression,
                )

                st.dataframe(
                    side_report,
                    use_container_width=True,
                    hide_index=True,
                )

            # =========================
            # DYNAMIC CONFLUENCE EXPLORER
            # =========================
            st.markdown("---")
            st.markdown("### 🔎 Confluence Explorer")

            st.caption(
                "Seleccioná dos variables para buscar combinaciones "
                "con mejor Profit Factor y suficiente cantidad de trades."
            )

            confluence_dimensions = {
                # Detector
                "Compression Score": "compression_score_bucket",
                "Range Ratio": "range_ratio_bucket",
                "ATR Ratio": "atr_ratio_bucket",
                "Volume Ratio": "volume_ratio_bucket",
                "Compression Volume": "volume_ratio_bucket",
                "Average Body Ratio": "avg_body_pct_bucket",
                "Compression Range %": "compression_range_pct_bucket",

                # Structure
                "Compression Quality": "compression_quality_label",
                "Compression Shape": "compression_shape",
                "Duration": "compression_duration_bucket",
                "Height": "compression_height_bucket",
                "Inside Ratio": "inside_ratio_bucket",
                "Total Touches": "total_touches_bucket",
                "Touch Balance": "touch_balance_bucket",
                "Touch Direction": "touch_imbalance_direction",
                "Upper Slope": "upper_slope_bucket",
                "Lower Slope": "lower_slope_bucket",
                "Slope Difference": "slope_difference_bucket",

                # Entry
                "Entry Distance": "entry_distance_bucket_analytics",
                "Entry vs Compression": "entry_vs_compression_bucket",
                "Entry vs Breakout": "entry_vs_breakout_bucket",
                "Late Entry": "late_entry_label",

                # Trade
                "Side": "side",
            }

            confluence_dimensions = {
                label: col
                for label, col in confluence_dimensions.items()
                if col in analytics_df.columns
            }

            if len(confluence_dimensions) < 2:
                st.info(
                    "At least two compression dimensions are required."
                )

            else:
                confluence_labels = list(
                    confluence_dimensions.keys()
                )

                cc1, cc2 = st.columns(2)

                with cc1:
                    first_dimension_label = st.selectbox(
                        "First dimension",
                        options=confluence_labels,
                        index=0,
                        key="compression_confluence_dimension_1",
                    )

                with cc2:
                    default_second_index = (
                        1 if len(confluence_labels) > 1 else 0
                    )

                    second_dimension_label = st.selectbox(
                        "Second dimension",
                        options=confluence_labels,
                        index=default_second_index,
                        key="compression_confluence_dimension_2",
                    )

                first_dimension = confluence_dimensions[
                    first_dimension_label
                ]

                second_dimension = confluence_dimensions[
                    second_dimension_label
                ]

                if first_dimension == second_dimension:
                    st.warning(
                        "Select two different dimensions."
                    )

                else:
                    confluence_report = build_compression_analytics_report(
                        analytics_df,
                        [
                            first_dimension,
                            second_dimension,
                        ],
                        min_trades=min_trades_compression,
                    )

                    if confluence_report.empty:
                        st.info(
                            "No combinations meet the minimum trade requirement."
                        )

                    else:
                        st.dataframe(
                            confluence_report,
                            use_container_width=True,
                            hide_index=True,
                        )

                        st.caption(
                            "Ordenado por Profit Factor, PnL total, "
                            "Winrate y cantidad de trades."
                        )

            # =========================
            # THREE-WAY DISCOVERY
            # =========================
            st.markdown("---")
            st.markdown("### 🧬 Quality × Shape × Entry Timing")

            three_way_cols = [
                "compression_quality_label",
                "compression_shape",
                "late_entry_label",
            ]

            if all(
                col in analytics_df.columns
                for col in three_way_cols
            ):
                three_way_report = build_compression_analytics_report(
                    analytics_df,
                    three_way_cols,
                    min_trades=min_trades_compression,
                )

                if three_way_report.empty:
                    st.info(
                        "Not enough data for the three-variable analysis."
                    )
                else:
                    st.dataframe(
                        three_way_report,
                        use_container_width=True,
                        hide_index=True,
                    )

            # =========================
            # BEST GROUPS
            # =========================
            st.markdown("---")
            st.markdown("### 🏆 Best Compression Groups")

            best_group_candidates = []

            best_group_dimensions = [
                # Detector
                "compression_score_bucket",
                "range_ratio_bucket",
                "atr_ratio_bucket",
                "volume_ratio_bucket",
                "avg_body_pct_bucket",
                "compression_range_pct_bucket",

                # Structure
                "compression_quality_label",
                "compression_shape",
                "compression_duration_bucket",
                "compression_height_bucket",
                "inside_ratio_bucket",
                "total_touches_bucket",
                "slope_difference_bucket",

                # Entry
                "late_entry_label",
            ]

            for dimension in best_group_dimensions:
                if dimension not in analytics_df.columns:
                    continue

                report = build_compression_analytics_report(
                    analytics_df,
                    [dimension],
                    min_trades=min_trades_compression,
                )

                if report.empty:
                    continue

                report = report.copy()
                report["dimension"] = dimension
                report["group_value"] = report[dimension].astype(str)

                best_group_candidates.append(
                    report[
                        [
                            "dimension",
                            "group_value",
                            "trades",
                            "winrate",
                            "avg_pnl",
                            "total_pnl",
                            "profit_factor",
                        ]
                    ]
                )

            if not best_group_candidates:
                st.info(
                    "No compression groups meet the minimum trade requirement."
                )

            else:
                best_groups_df = pd.concat(
                    best_group_candidates,
                    ignore_index=True,
                )

                best_groups_df = best_groups_df.sort_values(
                    [
                        "profit_factor",
                        "total_pnl",
                        "trades",
                    ],
                    ascending=[False, False, False],
                    na_position="last",
                )

                st.dataframe(
                    best_groups_df.head(30),
                    use_container_width=True,
                    hide_index=True,
                )
    
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
                "WATCH_CREATED": "#0ea5e9",
                "WAIT_PULLBACK": "#eab308",
                "BREAKOUT_DETECTED": "#38bdf8",
                "WATCHING_COMPRESSION": "#a78bfa",
                "EXPIRED": "#ef4444",
            }.get(state, "#6b7280")
            
        def quality_color(label):
            return {
                "GOOD_SHAPE": "#22c55e",
                "OK_SHAPE": "#eab308",
                "BAD_SHAPE": "#ef4444",
            }.get(label, "#6b7280")


        def shape_stars(label):
            return {
                "GOOD_SHAPE": "⭐⭐⭐⭐☆",
                "OK_SHAPE": "⭐⭐⭐☆☆",
                "BAD_SHAPE": "⭐☆☆☆☆",
            }.get(label, "N/A")

        def card_html(row):
            state = row.get("state", "N/A")
            color = state_color(state)
            qcolor = score_color(row.get("compression_score", 0))
            
            quality = row.get("compression_quality_label", "N/A")
            quality_col = quality_color(quality)
            stars = shape_stars(quality)

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

                <div style="display:grid; grid-template-columns: repeat(4, 1fr); gap:14px;">
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
                        <div style="color:#94a3b8; font-size:12px;">Shape / Quality</div>

                        <div style="font-size:16px; font-weight:900; color:{quality_col};">
                            {quality}
                        </div>

                        <div style="color:#facc15; font-size:15px; margin-bottom:4px;">
                            {stars}
                        </div>

                        <div style="color:#cbd5e1;">
                            Shape: <b>{row.get("compression_shape", "N/A")}</b>
                        </div>

                        <div style="color:#cbd5e1;">
                            Height: <b>{fmt(row.get("compression_height_pct"))}%</b>
                        </div>

                        <div style="color:#cbd5e1;">
                            Duration: <b>{fmt(row.get("compression_duration"))}</b>
                        </div>

                        <div style="color:#cbd5e1;">
                            Inside: <b>{fmt(row.get("inside_ratio"))}</b>
                        </div>

                        <div style="color:#cbd5e1;">
                            Touches: <b>H {fmt(row.get("touches_high"))} / L {fmt(row.get("touches_low"))}</b>
                        </div>

                        <div style="color:#cbd5e1;">
                            Upper Slope: <b>{fmt(row.get("upper_slope"))}</b>
                        </div>

                        <div style="color:#cbd5e1;">
                            Lower Slope: <b>{fmt(row.get("lower_slope"))}</b>
                        </div>

                        <div style="color:#cbd5e1;">
                            Δ Slope: <b>{fmt(row.get("slope_difference"))}</b>
                        </div>
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

        c1, c2, c3, c4, c5, c6 = st.columns(6)

        c1.metric("ENTRY_READY", int(state_counts.get("ENTRY_READY", 0)))
        c2.metric("WATCH_CREATED", int(state_counts.get("WATCH_CREATED", 0)))
        c3.metric("WAIT_PULLBACK", int(state_counts.get("WAIT_PULLBACK", 0)))
        c4.metric("BREAKOUT", int(state_counts.get("BREAKOUT_DETECTED", 0)))
        c5.metric("WATCHING", int(state_counts.get("WATCHING_COMPRESSION", 0)))
        c6.metric("EXPIRED", int(state_counts.get("EXPIRED", 0)))

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
            "WATCH_CREATED",
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

            render_watch_history_card(history_df, selected_symbol)
            
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
                "compression_quality_label",
                "compression_shape",
                "compression_height_pct",
                "compression_duration",
                "inside_ratio",
                "touches_high",
                "touches_low",
                "upper_slope",
                "lower_slope",
                "slope_difference",
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
            
# ==========================================================
# TP / SL POST-TRADE REPLAY
# ==========================================================
with tab_tp_sl_replay:
    st.subheader("🧪 TP / SL Post-Trade Replay")

    replay_df = load_csv_cached(
        POST_TRADE_REPLAY_FILE
    )

    scenario_df = load_csv_cached(
        TP_SL_SCENARIOS_FILE
    )

    partial_df = load_csv_cached(
        PARTIAL_TP_SCENARIOS_FILE
    )

    missing_reports = []

    if replay_df.empty:
        missing_reports.append(
            "post_trade_replay.csv"
        )

    if scenario_df.empty:
        missing_reports.append(
            "tp_sl_scenarios.csv"
        )

    if partial_df.empty:
        missing_reports.append(
            "partial_tp_scenarios.csv"
        )

    if missing_reports:
        st.warning(
            "Missing or empty replay reports: "
            + ", ".join(missing_reports)
        )

        st.code(
            "python -m tools.analyze_post_trade_replay "
            "--hours 72",
            language="bash",
        )

    else:
        st.caption(
            "Replay basado en velas de 1 minuto: "
            "Trade Price para TP y Mark Price para SL."
        )

        replay = replay_df.copy()
        scenarios = scenario_df.copy()
        partials = partial_df.copy()

        # ==========================================
        # NORMALIZE TIMESTAMPS
        # ==========================================
        for frame in [replay, scenarios, partials]:
            for column in ["entry_ts", "exit_ts"]:
                if column in frame.columns:
                    frame[column] = pd.to_datetime(
                        frame[column],
                        utc=True,
                        errors="coerce",
                    )

        # ==========================================
        # NORMALIZE NUMERIC COLUMNS
        # ==========================================
        replay_numeric = [
            "pnl",
            "original_tp_pct",
            "structural_sl_risk_pct",
            "post_max_favorable_pct",
            "after_exit_max_favorable_pct",
            "extra_move_after_exit_pct",
        ]

        for column in replay_numeric:
            if column in replay.columns:
                replay[column] = pd.to_numeric(
                    replay[column],
                    errors="coerce",
                )

        scenario_numeric = [
            "sl_buffer_pct",
            "tp_target_pct",
            "structural_risk_pct",
            "simulated_pnl_pct",
        ]

        for column in scenario_numeric:
            if column in scenarios.columns:
                scenarios[column] = pd.to_numeric(
                    scenarios[column],
                    errors="coerce",
                )

        partial_numeric = [
            "original_pnl",
            "structural_risk_pct",
            "targets_hit",
            "gross_pnl_pct",
            "net_pnl_pct",
            "minutes_in_trade",
        ]

        for column in partial_numeric:
            if column in partials.columns:
                partials[column] = pd.to_numeric(
                    partials[column],
                    errors="coerce",
                )

        # ==========================================
        # OBSERVATION WINDOW
        # ==========================================
        observation_hours = st.selectbox(
            "Minimum completed observation window",
            options=[24, 48, 72],
            index=2,
            format_func=lambda value: f"{value} hours",
            key="tp_sl_replay_observation_hours",
        )

        cutoff = (
            pd.Timestamp.now(tz="UTC")
            - pd.Timedelta(
                hours=observation_hours
            )
        )

        mature_replay = replay[
            replay["exit_ts"] <= cutoff
        ].copy()

        mature_scenarios = scenarios[
            scenarios["exit_ts"] <= cutoff
        ].copy()

        mature_partials = partials[
            partials["exit_ts"] <= cutoff
        ].copy()

        # ==========================================
        # REPLAY COVERAGE
        # ==========================================
        if "saved_by_compression_level" in mature_replay.columns:
            saved_mask = normalize_replay_bool(
                mature_replay[
                    "saved_by_compression_level"
                ]
            )
        else:
            saved_mask = pd.Series(
                False,
                index=mature_replay.index,
            )

        if "structural_result" in mature_replay.columns:
            structural_result = (
                mature_replay["structural_result"]
                .astype(str)
                .str.upper()
            )

            valid_structural_mask = ~structural_result.isin([
                "NO_COMPRESSION_LEVEL",
                "NAN",
                "NONE",
            ])

            unresolved_mask = structural_result.eq(
                "UNRESOLVED"
            )
        else:
            valid_structural_mask = pd.Series(
                False,
                index=mature_replay.index,
            )

            unresolved_mask = pd.Series(
                False,
                index=mature_replay.index,
            )

        total_trades = len(mature_replay)
        valid_structural_count = int(
            valid_structural_mask.sum()
        )
        saved_sl_count = int(saved_mask.sum())
        unresolved_count = int(
            unresolved_mask.sum()
        )

        c1, c2, c3, c4 = st.columns(4)

        c1.metric(
            "Mature trades",
            total_trades,
        )

        c2.metric(
            "With structural level",
            valid_structural_count,
        )

        c3.metric(
            "Original SL saved",
            saved_sl_count,
        )

        c4.metric(
            "Unresolved",
            unresolved_count,
        )

        st.caption(
            f"Only trades exited before "
            f"{cutoff.strftime('%Y-%m-%d %H:%M UTC')} "
            f"are included."
        )

        # ==========================================
        # STRUCTURAL RESULT SUMMARY
        # ==========================================
        st.markdown("### Structural Replay Results")

        if "structural_result" in mature_replay.columns:
            structural_summary = (
                mature_replay[
                    "structural_result"
                ]
                .fillna("UNKNOWN")
                .value_counts(dropna=False)
                .rename_axis("result")
                .reset_index(name="trades")
            )

            if total_trades > 0:
                structural_summary["pct"] = (
                    structural_summary["trades"]
                    / total_trades
                    * 100
                ).round(2)
            else:
                structural_summary["pct"] = 0.0

            st.dataframe(
                structural_summary,
                use_container_width=True,
                hide_index=True,
            )

        # ==========================================
        # RAW REPORT DIAGNOSTICS
        # ==========================================
        with st.expander("Replay report diagnostics"):
            d1, d2, d3 = st.columns(3)

            d1.metric(
                "Replay rows",
                len(replay),
            )

            d2.metric(
                "TP/SL scenario rows",
                len(scenarios),
            )

            d3.metric(
                "Partial scenario rows",
                len(partials),
            )

            if "analysis_status" in replay.columns:
                status_summary = (
                    replay["analysis_status"]
                    .fillna("UNKNOWN")
                    .value_counts()
                    .rename_axis("status")
                    .reset_index(name="trades")
                )

                st.dataframe(
                    status_summary,
                    use_container_width=True,
                    hide_index=True,
                )