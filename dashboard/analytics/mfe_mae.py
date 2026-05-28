import pandas as pd


MFE_LEVELS = [0.25, 0.50, 1.00, 1.50, 2.00, 3.00]
SL_MFE_LEVELS = [0.40, 0.75, 1.00, 1.50]


def _safe_mean(df, col):
    if df.empty or col not in df.columns:
        return 0.0

    s = pd.to_numeric(df[col], errors="coerce").dropna()
    if s.empty:
        return 0.0

    return round(float(s.mean()), 4)


def _level_stats(df, col, levels):
    total = len(df)

    result = {}

    for level in levels:
        if total == 0 or col not in df.columns:
            count = 0
            pct = 0.0
        else:
            count = int((df[col] >= level).sum())
            pct = round(count / total * 100, 2)

        result[f">={level}%"] = {
            "count": count,
            "pct": pct,
        }

    return result


def build_mfe_mae_report(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}

    df = df.copy()

    if "max_favorable_pct" in df.columns:
        df["mfe"] = pd.to_numeric(df["max_favorable_pct"], errors="coerce")

    if "max_adverse_pct" in df.columns:
        df["mae"] = pd.to_numeric(df["max_adverse_pct"], errors="coerce")

    for col in ["mfe", "mae", "pnl"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    if not {"symbol", "exit_reason", "pnl", "mfe", "mae"}.issubset(df.columns):
        return {}

    tp_df = df[df["exit_reason"] == "TP"]
    sl_df = df[df["exit_reason"].isin(["SL", "BE_SL", "TRAILING_SL"])]

    by_symbol = (
        df.groupby("symbol")
        .agg(
            trades=("symbol", "count"),
            avg_mfe=("mfe", "mean"),
            median_mfe=("mfe", "median"),
            avg_mae=("mae", "mean"),
            avg_pnl=("pnl", "mean"),
            total_pnl=("pnl", "sum"),
        )
        .reset_index()
        .round(4)
    )

    capture_df = df[df["mfe"] > 0].copy()
    capture_df["capture_ratio"] = (capture_df["pnl"] / capture_df["mfe"] * 100).clip(-100, 100)

    return {
        "total_trades": int(len(df)),
        "mfe_distribution": _level_stats(df, "mfe", MFE_LEVELS),
        "sl_mfe_distribution": _level_stats(sl_df, "mfe", SL_MFE_LEVELS),
        "result_comparison": {
            "TP": {
                "trades": int(len(tp_df)),
                "avg_mfe": _safe_mean(tp_df, "mfe"),
                "avg_mae": _safe_mean(tp_df, "mae"),
                "avg_pnl": _safe_mean(tp_df, "pnl"),
            },
            "SL": {
                "trades": int(len(sl_df)),
                "avg_mfe": _safe_mean(sl_df, "mfe"),
                "avg_mae": _safe_mean(sl_df, "mae"),
                "avg_pnl": _safe_mean(sl_df, "pnl"),
            },
        },
        "top_symbols_by_mfe": (
            by_symbol.sort_values("avg_mfe", ascending=False)
            .head(20)
            .to_dict(orient="records")
        ),
        "worst_symbols_by_mfe": (
            by_symbol.sort_values("avg_mfe", ascending=True)
            .head(20)
            .to_dict(orient="records")
        ),
        "capture": {
            "trades": int(len(capture_df)),
            "avg_capture_ratio": _safe_mean(capture_df, "capture_ratio"),
            "median_capture_ratio": round(float(capture_df["capture_ratio"].median()), 4)
            if not capture_df.empty else 0.0,
        },
    }