import pandas as pd
import plotly.graph_objects as go

from plotly.subplots import (
    make_subplots,
)

def _finite_number(
    value,
):
    if (
        value is None
        or pd.isna(value)
    ):
        return None

    try:
        value = float(value)
    except (TypeError, ValueError):
        return None

    if not pd.notna(value):
        return None

    return value


def _score_color(
    value,
):
    value = _finite_number(
        value
    )

    if value is None:
        return "#64748b"

    if value >= 75:
        return "#00c087"

    if value >= 50:
        return "#ffd166"

    return "#f6465d"


def _format_panel_value(
    value,
    suffix="",
    decimals=2,
):
    value = _finite_number(
        value
    )

    if value is None:
        return "—"

    return (
        f"{value:.{decimals}f}"
        f"{suffix}"
    )
    
def _add_panel_score_bar(
    figure,
    label,
    value,
    y,
):
    numeric_value = _finite_number(
        value
    )

    color = _score_color(
        numeric_value
    )

    display_value = (
        _format_panel_value(
            numeric_value
        )
    )

    normalized_value = (
        max(
            0.0,
            min(
                numeric_value,
                100.0,
            ),
        )
        if numeric_value is not None
        else 0.0
    )

    bar_x0 = 0.775
    bar_x1 = 0.942

    filled_x1 = (
        bar_x0
        + (
            bar_x1
            - bar_x0
        )
        * normalized_value
        / 100
    )

    figure.add_annotation(
        x=bar_x0,
        y=y + 0.018,
        xref="paper",
        yref="paper",
        text=label,
        showarrow=False,
        xanchor="left",
        font={
            "size": 11,
            "color": "#cbd5e1",
        },
    )

    figure.add_annotation(
        x=0.978,
        y=y + 0.018,
        xref="paper",
        yref="paper",
        text=display_value,
        showarrow=False,
        xanchor="right",
        font={
            "size": 12,
            "color": color,
        },
    )

    figure.add_shape(
        type="rect",
        xref="paper",
        yref="paper",
        x0=bar_x0,
        x1=bar_x1,
        y0=y - 0.014,
        y1=y,
        line={
            "width": 0,
        },
        fillcolor="#273244",
        layer="above",
    )

    if numeric_value is not None:
        figure.add_shape(
            type="rect",
            xref="paper",
            yref="paper",
            x0=bar_x0,
            x1=filled_x1,
            y0=y - 0.014,
            y1=y,
            line={
                "width": 0,
            },
            fillcolor=color,
            layer="above",
        )


def _add_geometry_metrics_panel(
    figure,
    candidate,
    chart_symbol,
    chart_status,
):
    geometry = str(
        candidate.get(
            "geometry",
            "unknown",
        )
    ).replace(
        "_",
        " ",
    ).title()

    breakout_direction = (
        candidate.get(
            "breakout_direction"
        )
    )

    if (
        breakout_direction is not None
        and pd.notna(
            breakout_direction
        )
    ):
        breakout_direction = str(
            breakout_direction
        ).upper()
    else:
        breakout_direction = None

    if chart_status == "FORMING":
        status_text = "FORMING"
        status_color = "#00d4ff"

    elif breakout_direction == "UP":
        status_text = "BREAKOUT UP"
        status_color = "#00c087"

    elif breakout_direction == "DOWN":
        status_text = "BREAKOUT DOWN"
        status_color = "#f6465d"

    else:
        status_text = chart_status
        status_color = "#ffd166"

    breakout_score = _finite_number(
        candidate.get(
            "breakout_score"
        )
    )

    maturity = _finite_number(
        candidate.get(
            "pattern_progress_pct"
        )
    )

    if chart_status == "FORMING":
        insight = "Awaiting breakout"

    elif (
        breakout_score is not None
        and breakout_score >= 75
    ):
        insight = "Strong observed breakout"

    elif (
        breakout_score is not None
        and breakout_score >= 50
    ):
        insight = "Moderate observed breakout"

    elif breakout_score is not None:
        insight = "Weak observed breakout"

    else:
        insight = "Breakout not scored"

    if maturity is not None:
        if maturity < 40:
            maturity_text = "Early"
        elif maturity < 60:
            maturity_text = "Developing"
        elif maturity <= 90:
            maturity_text = "Mature"
        elif maturity <= 100:
            maturity_text = "Near apex"
        else:
            maturity_text = "Past apex"

        insight = (
            f"{insight} · "
            f"{maturity_text}"
        )

    panel_x0 = 0.755
    panel_x1 = 0.995

    figure.add_shape(
        type="rect",
        xref="paper",
        yref="paper",
        x0=panel_x0,
        x1=panel_x1,
        y0=0.035,
        y1=0.98,
        line={
            "color": status_color,
            "width": 2,
        },
        fillcolor="rgba(13,18,28,0.96)",
        layer="below",
    )

    figure.add_annotation(
        x=0.875,
        y=0.945,
        xref="paper",
        yref="paper",
        text="<b>GEOMETRY SCANNER</b>",
        showarrow=False,
        xanchor="center",
        font={
            "size": 16,
            "color": "#f8fafc",
        },
    )

    figure.add_shape(
        type="line",
        xref="paper",
        yref="paper",
        x0=panel_x0,
        x1=panel_x1,
        y0=0.91,
        y1=0.91,
        line={
            "color": "#334155",
            "width": 1,
        },
    )

    panel_rows = [
        (
            "SYMBOL",
            str(chart_symbol),
            "#f8fafc",
            0.865,
        ),
        (
            "PATTERN",
            geometry,
            "#f8fafc",
            0.82,
        ),
        (
            "STATUS",
            status_text,
            status_color,
            0.775,
        ),
    ]

    for (
        label,
        value,
        color,
        y,
    ) in panel_rows:
        figure.add_annotation(
            x=0.775,
            y=y,
            xref="paper",
            yref="paper",
            text=label,
            showarrow=False,
            xanchor="left",
            font={
                "size": 10,
                "color": "#94a3b8",
            },
        )

        figure.add_annotation(
            x=0.978,
            y=y,
            xref="paper",
            yref="paper",
            text=f"<b>{value}</b>",
            showarrow=False,
            xanchor="right",
            font={
                "size": 11,
                "color": color,
            },
        )

    figure.add_shape(
        type="line",
        xref="paper",
        yref="paper",
        x0=panel_x0,
        x1=panel_x1,
        y0=0.735,
        y1=0.735,
        line={
            "color": "#334155",
            "width": 1,
        },
    )

    _add_panel_score_bar(
        figure=figure,
        label="GEOMETRY SCORE",
        value=candidate.get(
            "confidence"
        ),
        y=0.68,
    )

    _add_panel_score_bar(
        figure=figure,
        label="BREAKOUT SCORE",
        value=candidate.get(
            "breakout_score"
        ),
        y=0.59,
    )

    _add_panel_score_bar(
        figure=figure,
        label="MATURITY",
        value=candidate.get(
            "pattern_progress_pct"
        ),
        y=0.50,
    )

    figure.add_shape(
        type="line",
        xref="paper",
        yref="paper",
        x0=panel_x0,
        x1=panel_x1,
        y0=0.445,
        y1=0.445,
        line={
            "color": "#334155",
            "width": 1,
        },
    )

    detail_rows = [
        (
            "VOLUME",
            _format_panel_value(
                candidate.get(
                    "breakout_volume_ratio"
                ),
                suffix="x",
            ),
        ),
        (
            "ATR EXTENSION",
            _format_panel_value(
                candidate.get(
                    "breakout_atr_extension"
                ),
                suffix=" ATR",
            ),
        ),
        (
            "CLOSE OUTSIDE",
            _format_panel_value(
                candidate.get(
                    "breakout_close_distance_pct"
                ),
                suffix="%",
                decimals=4,
            ),
        ),
        (
            "BODY OUTSIDE",
            _format_panel_value(
                candidate.get(
                    "breakout_body_distance_pct"
                ),
                suffix="%",
                decimals=4,
            ),
        ),
        (
            "PRICE POSITION",
            _format_panel_value(
                candidate.get(
                    "current_price_position_pct"
                ),
                suffix="%",
            ),
        ),
    ]

    detail_y_values = [
        0.395,
        0.345,
        0.295,
        0.245,
        0.195,
    ]

    for (
        label,
        value,
    ), y in zip(
        detail_rows,
        detail_y_values,
    ):
        figure.add_annotation(
            x=0.775,
            y=y,
            xref="paper",
            yref="paper",
            text=label,
            showarrow=False,
            xanchor="left",
            font={
                "size": 10,
                "color": "#94a3b8",
            },
        )

        figure.add_annotation(
            x=0.978,
            y=y,
            xref="paper",
            yref="paper",
            text=value,
            showarrow=False,
            xanchor="right",
            font={
                "size": 11,
                "color": "#e2e8f0",
            },
        )

    figure.add_shape(
        type="line",
        xref="paper",
        yref="paper",
        x0=panel_x0,
        x1=panel_x1,
        y0=0.145,
        y1=0.145,
        line={
            "color": "#334155",
            "width": 1,
        },
    )
    
    insight_color = (
        status_color
        if chart_status == "FORMING"
        else _score_color(
            breakout_score
        )
    )

    figure.add_annotation(
        x=0.775,
        y=0.105,
        xref="paper",
        yref="paper",
        text="INSIGHT",
        showarrow=False,
        xanchor="left",
        font={
            "size": 10,
            "color": "#94a3b8",
        },
    )

    figure.add_annotation(
        x=0.978,
        y=0.072,
        xref="paper",
        yref="paper",
        text=f"<b>{insight}</b>",
        showarrow=False,
        xanchor="right",
        font={
            "size": 10,
            "color": insight_color,
        },
    )


def build_geometry_scanner_chart(
    candles,
    candidate,
    context_before=15,
    context_after=5,
    flagpole_lookback=10,
):
    
    if candles is None or candles.empty:
        raise ValueError(
            "candles are required"
        )

    if hasattr(candidate, "to_dict"):
        candidate = candidate.to_dict()
    else:
        candidate = dict(candidate)
        
    chart_symbol = candidate.get(
        "symbol"
    )

    if (
        chart_symbol is None
        or pd.isna(chart_symbol)
    ):
        if "symbol" in candles.columns:
            chart_symbol = str(
                candles[
                    "symbol"
                ].iloc[0]
            )
        else:
            chart_symbol = "UNKNOWN"

    raw_status = candidate.get(
        "status"
    )

    if (
        raw_status is not None
        and pd.notna(raw_status)
    ):
        chart_status = str(
            raw_status
        ).upper()
    else:
        chart_status = (
            "BREAKOUT"
            if bool(
                candidate.get(
                    "breakout_detected",
                    False,
                )
            )
            else "FORMING"
        )

    start_index = int(
        candidate["start_index"]
    )

    end_index = int(
        candidate["end_index"]
    )

    first_index = max(
        0,
        start_index
        - int(context_before),
    )

    last_index = min(
        len(candles) - 1,
        end_index
        + int(context_after),
    )

    visible = (
        candles.iloc[
            first_index:
            last_index + 1
        ]
        .copy()
        .reset_index(drop=False)
        .rename(
            columns={
                "index": (
                    "source_index"
                )
            }
        )
    )

    visible["chart_time"] = (
        pd.to_datetime(
            visible["timestamp"],
            unit="ms",
            utc=True,
        )
    )

    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.78, 0.22],
    )

    figure.add_trace(
        go.Candlestick(
            x=visible["chart_time"],
            open=visible["open"],
            high=visible["high"],
            low=visible["low"],
            close=visible["close"],
            name="Price",
            increasing_line_color=(
                "#00c087"
            ),
            decreasing_line_color=(
                "#f6465d"
            ),
        ),
        row=1,
        col=1,
    )

    volume_colors = [
        (
            "#00c087"
            if close_price
            >= open_price
            else "#f6465d"
        )
        for open_price, close_price
        in zip(
            visible["open"],
            visible["close"],
        )
    ]

    figure.add_trace(
        go.Bar(
            x=visible["chart_time"],
            y=visible["volume"],
            marker_color=volume_colors,
            opacity=0.55,
            name="Volume",
        ),
        row=2,
        col=1,
    )

    pattern_start_time = (
        pd.to_datetime(
            int(
                candidate[
                    "start_timestamp"
                ]
            ),
            unit="ms",
            utc=True,
        )
    )

    pattern_end_time = (
        pd.to_datetime(
            int(
                candidate[
                    "end_timestamp"
                ]
            ),
            unit="ms",
            utc=True,
        )
    )

    figure.add_trace(
        go.Scatter(
            x=[
                pattern_start_time,
                pattern_end_time,
            ],
            y=[
                candidate[
                    "upper_start_price"
                ],
                candidate[
                    "upper_end_price"
                ],
            ],
            mode="lines",
            name="Upper boundary",
            line={
                "color": "#00d4ff",
                "width": 3,
            },
        ),
        row=1,
        col=1,
    )

    figure.add_trace(
        go.Scatter(
            x=[
                pattern_start_time,
                pattern_end_time,
            ],
            y=[
                candidate[
                    "lower_start_price"
                ],
                candidate[
                    "lower_end_price"
                ],
            ],
            mode="lines",
            name="Lower boundary",
            line={
                "color": "#ffd166",
                "width": 3,
            },
        ),
        row=1,
        col=1,
    )

    high_pivot_indexes = (
        candidate.get(
            "high_pivot_indexes"
        )
        or []
    )

    valid_high_indexes = [
        int(index)
        for index in high_pivot_indexes
        if (
            0
            <= int(index)
            < len(candles)
        )
    ]

    if valid_high_indexes:
        high_pivots = candles.iloc[
            valid_high_indexes
        ]

        figure.add_trace(
            go.Scatter(
                x=pd.to_datetime(
                    high_pivots[
                        "timestamp"
                    ],
                    unit="ms",
                    utc=True,
                ),
                y=high_pivots["high"],
                mode="markers",
                name="High pivots",
                marker={
                    "color": "#00d4ff",
                    "size": 10,
                    "symbol": (
                        "triangle-down"
                    ),
                },
            ),
            row=1,
            col=1,
        )

    low_pivot_indexes = (
        candidate.get(
            "low_pivot_indexes"
        )
        or []
    )

    valid_low_indexes = [
        int(index)
        for index in low_pivot_indexes
        if (
            0
            <= int(index)
            < len(candles)
        )
    ]

    if valid_low_indexes:
        low_pivots = candles.iloc[
            valid_low_indexes
        ]

        figure.add_trace(
            go.Scatter(
                x=pd.to_datetime(
                    low_pivots[
                        "timestamp"
                    ],
                    unit="ms",
                    utc=True,
                ),
                y=low_pivots["low"],
                mode="markers",
                name="Low pivots",
                marker={
                    "color": "#ffd166",
                    "size": 10,
                    "symbol": (
                        "triangle-up"
                    ),
                },
            ),
            row=1,
            col=1,
        )

    flagpole_start_index = max(
        0,
        start_index
        - int(flagpole_lookback),
    )

    flagpole_start_time = (
        pd.to_datetime(
            int(
                candles[
                    "timestamp"
                ].iloc[
                    flagpole_start_index
                ]
            ),
            unit="ms",
            utc=True,
        )
    )

    figure.add_vrect(
        x0=flagpole_start_time,
        x1=pattern_start_time,
        fillcolor="#7b61ff",
        opacity=0.08,
        line_width=0,
        annotation_text="Flagpole",
        annotation_position="top left",
        row=1,
        col=1,
    )

    figure.add_vrect(
        x0=pattern_start_time,
        x1=pattern_end_time,
        fillcolor="#00d4ff",
        opacity=0.07,
        line_width=0,
        annotation_text=(
            candidate["geometry"]
        ),
        annotation_position="top right",
        row=1,
        col=1,
    )

    breakout_timestamp = candidate.get(
        "breakout_timestamp"
    )

    breakout_price = candidate.get(
        "breakout_price"
    )

    has_breakout = (
        pd.notna(breakout_timestamp)
        and pd.notna(breakout_price)
    )

    if has_breakout:
        breakout_time = pd.to_datetime(
            int(breakout_timestamp),
            unit="ms",
            utc=True,
        )

        raw_breakout_direction = (
            candidate.get(
                "breakout_direction"
            )
        )

        if pd.notna(
            raw_breakout_direction
        ):
            breakout_direction = str(
                raw_breakout_direction
            ).upper()
        else:
            breakout_direction = (
                "UNKNOWN"
            )

        if breakout_direction == "UP":
            breakout_color = "#00c087"
            breakout_symbol = (
                "triangle-up"
            )
            breakout_text = (
                "Breakout UP"
            )

        elif breakout_direction == "DOWN":
            breakout_color = "#f6465d"
            breakout_symbol = (
                "triangle-down"
            )
            breakout_text = (
                "Breakout DOWN"
            )

        else:
            breakout_color = "#ffd166"
            breakout_symbol = "star"
            breakout_text = "Breakout"

        figure.add_trace(
            go.Scatter(
                x=[breakout_time],
                y=[float(breakout_price)],
                mode="markers+text",
                name=breakout_text,
                text=[breakout_text],
                textposition="top center",
                marker={
                    "color": breakout_color,
                    "size": 15,
                    "symbol": (
                        breakout_symbol
                    ),
                    "line": {
                        "color": (
                            breakout_color
                        ),
                        "width": 2,
                    },
                },
            ),
            row=1,
            col=1,
        )
        
    _add_geometry_metrics_panel(
        figure=figure,
        candidate=candidate,
        chart_symbol=chart_symbol,
        chart_status=chart_status,
    )
        
    figure.update_layout(
        template="plotly_dark",
        height=760,
        margin={
            "l": 20,
            "r": 20,
            "t": 80,
            "b": 20,
        },
        title={
            "text": (
                f"{chart_symbol} · "
                f"{candidate['geometry']} · "
                f"{chart_status} · "
                f"confidence "
                f"{candidate['confidence']:.2f} · "
                f"window "
                f"{candidate['window_size']}"
            ),
            "x": 0.01,
        },
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.02,
            "xanchor": "right",
            "x": 0.72,
        },
    )
    
    figure.update_xaxes(
        domain=[
            0.0,
            0.72,
        ],
    )

    figure.update_yaxes(
        title_text="Price",
        row=1,
        col=1,
    )

    figure.update_yaxes(
        title_text="Volume",
        row=2,
        col=1,
    )

    figure.update_xaxes(
        showgrid=True,
        gridcolor="rgba(255,255,255,0.06)",
    )

    figure.update_yaxes(
        showgrid=True,
        gridcolor="rgba(255,255,255,0.06)",
    )

    return figure