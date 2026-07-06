from datetime import timedelta
from typing import Optional

import pandas as pd
import plotly.graph_objects as go

from dashboard.services.trade_inspector_service import (
    TradeInspection,
)


def build_trade_inspector_chart(
    inspection: TradeInspection,
    interval_minutes: int = 30,
    timeframe: str = "30m",
) -> go.Figure:

    trade = inspection.trade
    candles = inspection.candles.copy()

    fig = go.Figure()

    if candles.empty:
        return fig

    # =========================
    # CANDLESTICKS
    # =========================

    fig.add_trace(
        go.Candlestick(
            x=candles["open_ts"],
            open=candles["open"],
            high=candles["high"],
            low=candles["low"],
            close=candles["close"],
            name=trade.symbol,
            increasing_line_color="#22c55e",
            decreasing_line_color="#ef4444",
            increasing_fillcolor="#22c55e",
            decreasing_fillcolor="#ef4444",
        )
    )

    chart_start = candles["open_ts"].min()
    chart_end = candles["close_ts"].max()

    compression_start = _resolve_compression_start(
        inspection=inspection,
        interval_minutes=interval_minutes,
    )

    compression_end = (
        trade.breakout_ts
        or trade.entry_ready_ts
        or trade.entry_ts
        or chart_end
    )

    # =========================
    # COMPRESSION BOX
    # =========================

    if (
        trade.compression_low is not None
        and trade.compression_high is not None
    ):
        fig.add_shape(
            type="rect",
            x0=compression_start or chart_start,
            x1=compression_end,
            y0=trade.compression_low,
            y1=trade.compression_high,
            fillcolor="rgba(139, 92, 246, 0.18)",
            line={
                "color": "#a78bfa",
                "width": 1.5,
                "dash": "dash",
            },
            layer="below",
        )

        fig.add_annotation(
            x=compression_start or chart_start,
            y=trade.compression_high,
            text="Compression",
            showarrow=False,
            xanchor="left",
            yanchor="bottom",
            font={
                "color": "#c4b5fd",
                "size": 11,
            },
        )

    # =========================
    # PRICE LEVELS
    # =========================

    _add_price_level(
        fig,
        price=trade.compression_high,
        label="Compression high",
        color="#38bdf8",
        dash="dash",
    )

    _add_price_level(
        fig,
        price=trade.compression_low,
        label="Compression low",
        color="#a78bfa",
        dash="dash",
    )

    _add_price_level(
        fig,
        price=trade.breakout_price,
        label="Breakout price",
        color="#f59e0b",
        dash="dot",
    )

    _add_price_level(
        fig,
        price=trade.entry_ready_price,
        label="Entry ready price",
        color="#facc15",
        dash="dot",
    )

    _add_price_level(
        fig,
        price=trade.entry_price,
        label="Entry",
        color="#ffffff",
        dash="solid",
        width=2,
    )

    _add_price_level(
        fig,
        price=trade.tp,
        label="TP",
        color="#22c55e",
        dash="dash",
    )

    _add_price_level(
        fig,
        price=trade.sl,
        label="SL",
        color="#ef4444",
        dash="dash",
    )

    # =========================
    # EVENT MARKERS
    # =========================

    _add_event_marker(
        fig,
        timestamp=trade.compression_created_ts,
        price=trade.compression_high,
        label="Watch created",
        color="#a78bfa",
        symbol="diamond",
    )

    _add_event_marker(
        fig,
        timestamp=trade.breakout_ts,
        price=(
            trade.breakout_price
            or trade.breakout_high
        ),
        label="Breakout",
        color="#f59e0b",
        symbol="triangle-up",
    )

    _add_event_marker(
        fig,
        timestamp=trade.pullback_first_ts,
        price=trade.pullback_price,
        label="First pullback",
        color="#fb923c",
        symbol="triangle-down",
    )

    _add_event_marker(
        fig,
        timestamp=trade.pullback_valid_ts,
        price=trade.pullback_price,
        label="Valid pullback",
        color="#14b8a6",
        symbol="diamond",
    )

    _add_event_marker(
        fig,
        timestamp=trade.entry_ready_ts,
        price=trade.entry_ready_price,
        label="ENTRY_READY",
        color="#facc15",
        symbol="star",
    )

    _add_event_marker(
        fig,
        timestamp=trade.entry_ts,
        price=trade.entry_price,
        label="Entry",
        color="#ffffff",
        symbol="circle",
        size=12,
    )

    _add_event_marker(
        fig,
        timestamp=trade.exit_ts,
        price=trade.exit_price,
        label=trade.exit_reason or "Exit",
        color=(
            "#22c55e"
            if _is_profitable_trade(inspection)
            else "#ef4444"
        ),
        symbol="x",
        size=13,
    )

    # =========================
    # LAYOUT
    # =========================

    fig.update_layout(
        title={
            "text": (
                f"{trade.symbol} · {trade.side} · "
                f"{trade.status} · {timeframe}"
            ),
            "x": 0.01,
            "xanchor": "left",
        },
        height=650,
        margin={
            "l": 20,
            "r": 90,
            "t": 60,
            "b": 30,
        },
        paper_bgcolor="#0e1117",
        plot_bgcolor="#0e1117",
        font={
            "color": "#d1d5db",
        },
        hovermode="x unified",
        showlegend=True,
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.02,
            "xanchor": "right",
            "x": 1,
        },
        xaxis={
            "rangeslider": {
                "visible": False,
            },
            "showgrid": True,
            "gridcolor": "#1f2937",
        },
        yaxis={
            "side": "right",
            "showgrid": True,
            "gridcolor": "#1f2937",
            "tickformat": ".8f",
        },
    )

    return fig


def _resolve_compression_start(
    inspection: TradeInspection,
    interval_minutes: int,
) -> Optional[pd.Timestamp]:

    trade = inspection.trade

    if trade.compression_start_ts is not None:
        return trade.compression_start_ts

    if (
        trade.compression_created_ts is not None
        and trade.compression_duration is not None
        and trade.compression_duration > 0
    ):
        candles_back = max(
            trade.compression_duration - 1,
            0,
        )

        return (
            trade.compression_created_ts
            - timedelta(
                minutes=interval_minutes * candles_back
            )
        )

    return trade.compression_created_ts


def _add_price_level(
    fig: go.Figure,
    price: Optional[float],
    label: str,
    color: str,
    dash: str,
    width: float = 1,
) -> None:

    if price is None:
        return

    fig.add_hline(
        y=price,
        line_color=color,
        line_dash=dash,
        line_width=width,
        annotation_text=label,
        annotation_position="right",
        annotation_font_color=color,
    )


def _add_event_marker(
    fig: go.Figure,
    timestamp: Optional[pd.Timestamp],
    price: Optional[float],
    label: str,
    color: str,
    symbol: str,
    size: int = 10,
) -> None:

    if timestamp is None or price is None:
        return

    fig.add_trace(
        go.Scatter(
            x=[timestamp],
            y=[price],
            mode="markers+text",
            name=label,
            text=[label],
            textposition="top center",
            marker={
                "color": color,
                "size": size,
                "symbol": symbol,
                "line": {
                    "color": "#0e1117",
                    "width": 1,
                },
            },
            hovertemplate=(
                f"<b>{label}</b><br>"
                "%{x}<br>"
                "Price: %{y}<extra></extra>"
            ),
        )
    )


def _is_profitable_trade(
    inspection: TradeInspection,
) -> bool:

    pnl_pct = inspection.trade.pnl_pct

    if pnl_pct is None:
        return False

    return pnl_pct > 0