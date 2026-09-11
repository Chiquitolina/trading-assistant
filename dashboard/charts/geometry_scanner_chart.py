import pandas as pd
import plotly.graph_objects as go

from plotly.subplots import (
    make_subplots,
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
        annotation_text="Flagpole window",
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
        annotation_position="top left",
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

        figure.add_trace(
            go.Scatter(
                x=[breakout_time],
                y=[float(breakout_price)],
                mode="markers+text",
                name="Observed breakout",
                text=["Breakout"],
                textposition="top center",
                marker={
                    "color": "#00c087",
                    "size": 14,
                    "symbol": "star",
                },
            ),
            row=1,
            col=1,
        )
        
    figure.update_layout(
        template="plotly_dark",
        height=720,
        margin={
            "l": 20,
            "r": 20,
            "t": 80,
            "b": 20,
        },
        title={
            "text": (
                f"{candidate['geometry']} · "
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
            "x": 1,
        },
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