import pandas as pd


SHIFT_TYPES = [
    "bullish_value_shift",
    "bullish_extreme_shift",
    "bearish_value_shift",
    "bearish_extreme_shift",
]


def direction_performance(df: pd.DataFrame) -> pd.DataFrame:
    data = df[
        (df["direction_changed"] == True) &
        (df["direction_15m"].isin(["up", "down"])) &
        (df["fe_pct"].notna()) &
        (df["ae_pct"].notna())
    ].copy()

    if data.empty:
        return pd.DataFrame()

    summary = (
        data
        .groupby("direction_15m")
        .agg(
            signals=("direction_15m", "count"),
            avg_fe_pct=("fe_pct", "mean"),
            avg_ae_pct=("ae_pct", "mean"),
            max_fe_pct=("fe_pct", "max"),
            max_ae_pct=("ae_pct", "max"),
            avg_window_bars=("direction_window_bars", "mean"),
        )
        .reset_index()
    )

    return round_numeric(summary)


def momentum_performance(df: pd.DataFrame) -> pd.DataFrame:
    data = df[df["mom_fe_pct"].notna()].copy()

    if data.empty:
        return pd.DataFrame()

    summary = (
        data
        .groupby("momentum_5m")
        .agg(
            signals=("momentum_5m", "count"),
            avg_fe=("mom_fe_pct", "mean"),
            avg_ae=("mom_ae_pct", "mean"),
            max_fe=("mom_fe_pct", "max"),
            max_ae=("mom_ae_pct", "max"),
        )
        .reset_index()
    )

    summary["edge"] = summary["avg_fe"] - summary["avg_ae"]

    return round_numeric(summary).sort_values("edge", ascending=False)


def trend_performance(df: pd.DataFrame) -> pd.DataFrame:
    data = df[
        (df["trend_changed"] == True) &
        (df["trend_1h"].isin(["bullish", "bearish", "neutral"])) &
        (df["trend_fe_pct"].notna()) &
        (df["trend_ae_pct"].notna())
    ].copy()

    if data.empty:
        return pd.DataFrame()

    summary = (
        data
        .groupby("trend_1h")
        .agg(
            signals=("trend_1h", "count"),
            avg_fe_pct=("trend_fe_pct", "mean"),
            avg_ae_pct=("trend_ae_pct", "mean"),
            max_fe_pct=("trend_fe_pct", "max"),
            max_ae_pct=("trend_ae_pct", "max"),
            avg_window_bars=("trend_window_bars", "mean"),
        )
        .reset_index()
    )

    summary["edge"] = summary["avg_fe_pct"] - summary["avg_ae_pct"]

    return round_numeric(summary).sort_values("edge", ascending=False)


def shift_performance(df: pd.DataFrame) -> pd.DataFrame:
    data = df[
        (df["trend_shift"].isin(SHIFT_TYPES)) &
        (df["shift_fe_pct"].notna()) &
        (df["shift_ae_pct"].notna())
    ].copy()

    if data.empty:
        return pd.DataFrame()

    summary = (
        data
        .groupby("trend_shift")
        .agg(
            signals=("trend_shift", "count"),
            avg_fe_pct=("shift_fe_pct", "mean"),
            avg_ae_pct=("shift_ae_pct", "mean"),
            max_fe_pct=("shift_fe_pct", "max"),
            max_ae_pct=("shift_ae_pct", "max"),
            avg_window_bars=("shift_window_bars", "mean"),
        )
        .reset_index()
    )

    summary["edge"] = summary["avg_fe_pct"] - summary["avg_ae_pct"]

    return round_numeric(summary).sort_values("edge", ascending=False)


def shift_frequency(df: pd.DataFrame) -> pd.DataFrame:
    data = df[df["trend_shift"].isin(SHIFT_TYPES)].copy()

    if data.empty:
        return pd.DataFrame()

    data["bars_since_prev"] = data.index.to_series().diff()

    summary = (
        data
        .groupby("trend_shift")
        .agg(
            signals=("trend_shift", "count"),
            avg_bars_between=("bars_since_prev", "mean"),
            min_bars_between=("bars_since_prev", "min"),
            max_bars_between=("bars_since_prev", "max"),
        )
        .reset_index()
    )

    return round_numeric(summary)


def combined_signal_performance(
    df: pd.DataFrame,
    min_signals: int = 30,
) -> pd.DataFrame:
    data = df[
        (df["mom_fe_pct"].notna()) &
        (df["trend_1h"].notna()) &
        (df["direction_15m"].notna())
    ].copy()

    if data.empty:
        return pd.DataFrame()

    summary = (
        data
        .groupby(["trend_1h", "direction_15m", "momentum_5m"])
        .agg(
            signals=("momentum_5m", "count"),
            avg_fe=("mom_fe_pct", "mean"),
            avg_ae=("mom_ae_pct", "mean"),
        )
        .reset_index()
    )

    summary["edge"] = summary["avg_fe"] - summary["avg_ae"]
    summary = summary[summary["signals"] > min_signals]

    return round_numeric(summary).sort_values("edge", ascending=False)


def regime_summary(df: pd.DataFrame) -> pd.DataFrame:
    summary = (
        df
        .groupby("regime")
        .agg(
            bars=("regime", "count"),
            trend_changes=("trend_changed", "sum"),
            direction_changes=("direction_changed", "sum"),
            shifts=("trend_shift", lambda x: x.isin(SHIFT_TYPES).sum()),
        )
        .reset_index()
    )

    summary["bars_pct"] = (
        summary["bars"] / summary["bars"].sum() * 100
    )

    return round_numeric(summary).sort_values("bars", ascending=False)

def regime_shift_performance(df: pd.DataFrame) -> pd.DataFrame:
    data = df[
        (df["regime"].notna()) &
        (df["trend_shift"].isin(SHIFT_TYPES)) &
        (df["shift_fe_pct"].notna()) &
        (df["shift_ae_pct"].notna())
    ].copy()

    if data.empty:
        return pd.DataFrame()

    summary = (
        data
        .groupby(["regime", "trend_shift"])
        .agg(
            signals=("trend_shift", "count"),
            avg_fe_pct=("shift_fe_pct", "mean"),
            avg_ae_pct=("shift_ae_pct", "mean"),
            max_fe_pct=("shift_fe_pct", "max"),
            max_ae_pct=("shift_ae_pct", "max"),
            avg_window_bars=("shift_window_bars", "mean"),
        )
        .reset_index()
    )

    summary["edge"] = summary["avg_fe_pct"] - summary["avg_ae_pct"]

    return round_numeric(summary).sort_values("edge", ascending=False)


def regime_momentum_performance(
    df: pd.DataFrame,
    min_signals: int = 30,
) -> pd.DataFrame:
    data = df[
        (df["regime"].notna()) &
        (df["momentum_5m"].notna()) &
        (df["mom_fe_pct"].notna()) &
        (df["mom_ae_pct"].notna())
    ].copy()

    if data.empty:
        return pd.DataFrame()

    summary = (
        data
        .groupby(["regime", "momentum_5m"])
        .agg(
            signals=("momentum_5m", "count"),
            avg_fe=("mom_fe_pct", "mean"),
            avg_ae=("mom_ae_pct", "mean"),
            max_fe=("mom_fe_pct", "max"),
            max_ae=("mom_ae_pct", "max"),
        )
        .reset_index()
    )

    summary["edge"] = summary["avg_fe"] - summary["avg_ae"]
    summary = summary[summary["signals"] >= min_signals]

    return round_numeric(summary).sort_values("edge", ascending=False)


def regime_combo_performance(
    df: pd.DataFrame,
    min_signals: int = 20,
) -> pd.DataFrame:
    data = df[
        (df["regime"].notna()) &
        (df["trend_1h"].notna()) &
        (df["direction_15m"].notna()) &
        (df["momentum_5m"].notna()) &
        (df["mom_fe_pct"].notna()) &
        (df["mom_ae_pct"].notna())
    ].copy()

    if data.empty:
        return pd.DataFrame()

    summary = (
        data
        .groupby(["regime", "trend_1h", "direction_15m", "momentum_5m"])
        .agg(
            signals=("momentum_5m", "count"),
            avg_fe=("mom_fe_pct", "mean"),
            avg_ae=("mom_ae_pct", "mean"),
            max_fe=("mom_fe_pct", "max"),
            max_ae=("mom_ae_pct", "max"),
        )
        .reset_index()
    )

    summary["edge"] = summary["avg_fe"] - summary["avg_ae"]
    summary = summary[summary["signals"] >= min_signals]

    return round_numeric(summary).sort_values("edge", ascending=False)

def round_numeric(df: pd.DataFrame, decimals: int = 3) -> pd.DataFrame:
    df = df.copy()

    for col in df.columns:
        if pd.api.types.is_numeric_dtype(df[col]):
            df[col] = df[col].round(decimals)

    return df