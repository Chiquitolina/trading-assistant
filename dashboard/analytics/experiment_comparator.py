import json

import numpy as np
import pandas as pd


OUTCOMES = {"TP", "SL"}


def _numeric(series):
    if series is None:
        return pd.Series(dtype=float)
    return pd.to_numeric(
        series.astype(str).str.replace("%", "", regex=False),
        errors="coerce",
    )


def _first_available(df, names, default=None):
    for name in names:
        if name in df.columns:
            return df[name]
    return pd.Series(default, index=df.index)


def normalize_experiment_trades(df, source, timezone):
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy().reset_index(drop=True)
    out["experiment_source"] = source
    out["source_row"] = out.index.astype(int)
    out["symbol"] = _first_available(
        out, ["symbol"], ""
    ).fillna("").astype(str).str.upper().str.strip()
    out["side"] = _first_available(
        out, ["side"], ""
    ).fillna("").astype(str).str.upper().str.strip()

    entry_raw = _first_available(
        out,
        ["entry_ts", "entry_ts_dt", "opened_ts", "signal_ts"],
    )
    exit_raw = _first_available(
        out,
        ["exit_ts", "exit_ts_dt", "closed_ts"],
    )
    out["comparison_entry_ts"] = pd.to_datetime(
        entry_raw, utc=True, errors="coerce"
    )
    out["comparison_exit_ts"] = pd.to_datetime(
        exit_raw, utc=True, errors="coerce"
    )
    try:
        out["comparison_entry_local"] = out[
            "comparison_entry_ts"
        ].dt.tz_convert(timezone)
        out["comparison_exit_local"] = out[
            "comparison_exit_ts"
        ].dt.tz_convert(timezone)
    except Exception:
        out["comparison_entry_local"] = out["comparison_entry_ts"]
        out["comparison_exit_local"] = out["comparison_exit_ts"]

    out["comparison_date"] = out[
        "comparison_entry_local"
    ].dt.date
    out["pnl"] = _numeric(_first_available(out, ["pnl"]))
    out["pnl_usd"] = _numeric(
        _first_available(out, ["pnl_usd"])
    )
    out["fees"] = _numeric(_first_available(out, ["fees"], 0)).fillna(0)
    for target, alternatives in {
        "comparison_entry_price": ["real_entry", "entry", "entry_price"],
        "comparison_compression_high": ["compression_high"],
        "comparison_compression_low": ["compression_low"],
        "comparison_mfe": ["max_favorable_pct", "mfe"],
        "comparison_mae": ["max_adverse_pct", "mae"],
        "comparison_notional": ["notional", "position_notional"],
    }.items():
        out[target] = _numeric(
            _first_available(out, alternatives)
        )
    if out["comparison_notional"].isna().all():
        quantity = _numeric(
            _first_available(out, ["quantity", "qty", "position_qty"])
        )
        out["comparison_notional"] = (
            quantity.abs() * out["comparison_entry_price"]
        )

    exit_reason = _first_available(
        out, ["exit_reason"], ""
    ).fillna("").astype(str).str.upper().str.strip()
    out["comparison_outcome"] = np.select(
        [
            exit_reason.str.contains("TP|TAKE.?PROFIT", regex=True),
            exit_reason.str.contains("SL|STOP", regex=True),
        ],
        ["TP", "SL"],
        default=np.where(out["pnl"] > 0, "TP", "SL"),
    )

    selected_lookback = pd.Series(
        pd.NA,
        index=out.index,
        dtype="Float64",
    )

    for candidate_column in [
        "compression_selected_lookback",
        "selected_lookback",
        "compression_lookback",
        "compression_duration",
    ]:
        if candidate_column not in out.columns:
            continue

        selected_lookback = selected_lookback.fillna(
            pd.to_numeric(
                out[candidate_column],
                errors="coerce",
            )
        )
    out["comparison_selected_lookback"] = pd.to_numeric(
        selected_lookback, errors="coerce"
    ).astype("Int64")
    out["comparison_candidates_json"] = _first_available(
        out,
        ["compression_candidates_json", "candidates_json"],
        "",
    ).fillna("").astype(str)

    if "trade_id" in out.columns:
        trade_id = out["trade_id"].astype(str)
        valid_trade_id = (
            out["trade_id"].notna()
            & trade_id.ne("")
            & trade_id.ne("nan")
        )
    else:
        trade_id = pd.Series("", index=out.index)
        valid_trade_id = pd.Series(False, index=out.index)

    fallback_key = (
        out["symbol"]
        + "_"
        + out["comparison_entry_ts"].astype(str)
        + "_"
        + out["side"]
    )
    out["comparison_trade_key"] = np.where(
        valid_trade_id,
        source + "_" + trade_id,
        source + "_" + fallback_key,
    )
    out = out.dropna(subset=["comparison_entry_ts"])
    out = out.drop_duplicates(
        subset=["comparison_trade_key"], keep="first"
    )
    return out.reset_index(drop=True)


def calculate_strategy_summary(df):
    if df is None or df.empty:
        return {
            "trades": 0, "tp": 0, "sl": 0, "winrate": np.nan,
            "profit_factor": np.nan, "total_pnl": 0.0,
            "avg_pnl": np.nan, "expectancy": np.nan,
            "fees": 0.0, "fee_impact_pct": np.nan,
            "avg_win": np.nan, "avg_loss": np.nan,
            "max_drawdown": 0.0,
        }

    pnl = _numeric(df["pnl"]).dropna()
    ordered = df.assign(_pnl=_numeric(df["pnl"])).sort_values(
        "comparison_entry_ts"
    )
    equity = ordered["_pnl"].fillna(0).cumsum()
    drawdown = equity - equity.cummax()
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    gross_profit = wins.sum()
    gross_loss = abs(losses.sum())
    fees = _numeric(df.get("fees", pd.Series(0, index=df.index))).fillna(0).sum()
    gross_before_fees = pnl.sum() + fees
    return {
        "trades": int(len(df)),
        "tp": int((df["comparison_outcome"] == "TP").sum()),
        "sl": int((df["comparison_outcome"] == "SL").sum()),
        "winrate": float((pnl > 0).mean() * 100) if len(pnl) else np.nan,
        "profit_factor": (
            float(gross_profit / gross_loss)
            if gross_loss > 0 else (np.inf if gross_profit > 0 else np.nan)
        ),
        "total_pnl": float(pnl.sum()),
        "avg_pnl": float(pnl.mean()) if len(pnl) else np.nan,
        "expectancy": float(pnl.mean()) if len(pnl) else np.nan,
        "fees": float(fees),
        "fee_impact_pct": (
            float(fees / gross_before_fees * 100)
            if gross_before_fees > 0 else np.nan
        ),
        "avg_win": float(wins.mean()) if len(wins) else np.nan,
        "avg_loss": float(losses.mean()) if len(losses) else np.nan,
        "max_drawdown": float(drawdown.min()) if len(drawdown) else 0.0,
    }


def build_overview_report(original_df, dynamic_df):
    return pd.DataFrame([
        {"strategy": "Original", **calculate_strategy_summary(original_df)},
        {"strategy": "Dynamic X4", **calculate_strategy_summary(dynamic_df)},
    ])


def match_trade_opportunities(original_df, dynamic_df, tolerance_minutes=60):
    original = original_df.reset_index(drop=True)
    dynamic = dynamic_df.reset_index(drop=True)
    tolerance = pd.Timedelta(minutes=int(tolerance_minutes))
    candidates = []

    for original_idx, original_row in original.iterrows():
        compatible = dynamic[
            dynamic["symbol"].eq(original_row["symbol"])
            & dynamic["side"].eq(original_row["side"])
        ]
        for dynamic_idx, dynamic_row in compatible.iterrows():
            difference = abs(
                dynamic_row["comparison_entry_ts"]
                - original_row["comparison_entry_ts"]
            )
            if difference <= tolerance:
                candidates.append(
                    (difference, original_idx, dynamic_idx)
                )

    candidates.sort(key=lambda item: item[0])
    used_original = set()
    used_dynamic = set()
    pairs = []
    for difference, original_idx, dynamic_idx in candidates:
        if original_idx in used_original or dynamic_idx in used_dynamic:
            continue
        used_original.add(original_idx)
        used_dynamic.add(dynamic_idx)
        original_row = original.iloc[original_idx]
        dynamic_row = dynamic.iloc[dynamic_idx]
        pairs.append({
            "match_id": f"M{len(pairs) + 1:05d}",
            "symbol": original_row["symbol"],
            "side": original_row["side"],
            "original_index": original_idx,
            "dynamic_index": dynamic_idx,
            "original_trade_key": original_row["comparison_trade_key"],
            "dynamic_trade_key": dynamic_row["comparison_trade_key"],
            "original_entry_ts": original_row["comparison_entry_ts"],
            "dynamic_entry_ts": dynamic_row["comparison_entry_ts"],
            "entry_delta_minutes": difference.total_seconds() / 60,
            "entry_price_delta_pct": (
                (
                    dynamic_row["comparison_entry_price"]
                    - original_row["comparison_entry_price"]
                )
                / original_row["comparison_entry_price"]
                * 100
                if pd.notna(original_row["comparison_entry_price"])
                and original_row["comparison_entry_price"] != 0
                else np.nan
            ),
            "compression_high_delta_pct": (
                (
                    dynamic_row["comparison_compression_high"]
                    - original_row["comparison_compression_high"]
                )
                / original_row["comparison_compression_high"]
                * 100
                if pd.notna(original_row["comparison_compression_high"])
                and original_row["comparison_compression_high"] != 0
                else np.nan
            ),
            "compression_low_delta_pct": (
                (
                    dynamic_row["comparison_compression_low"]
                    - original_row["comparison_compression_low"]
                )
                / original_row["comparison_compression_low"]
                * 100
                if pd.notna(original_row["comparison_compression_low"])
                and original_row["comparison_compression_low"] != 0
                else np.nan
            ),
            "original_outcome": original_row["comparison_outcome"],
            "dynamic_outcome": dynamic_row["comparison_outcome"],
            "original_pnl": original_row["pnl"],
            "dynamic_pnl": dynamic_row["pnl"],
            "delta_pnl": dynamic_row["pnl"] - original_row["pnl"],
            "original_mfe": original_row["comparison_mfe"],
            "dynamic_mfe": dynamic_row["comparison_mfe"],
            "delta_mfe": (
                dynamic_row["comparison_mfe"]
                - original_row["comparison_mfe"]
            ),
            "original_mae": original_row["comparison_mae"],
            "dynamic_mae": dynamic_row["comparison_mae"],
            "delta_mae": (
                dynamic_row["comparison_mae"]
                - original_row["comparison_mae"]
            ),
            "dynamic_lookback": dynamic_row[
                "comparison_selected_lookback"
            ],
        })

    matched = pd.DataFrame(pairs)
    original_only = original.loc[
        ~original.index.isin(used_original)
    ].copy().reset_index(drop=True)
    dynamic_only = dynamic.loc[
        ~dynamic.index.isin(used_dynamic)
    ].copy().reset_index(drop=True)
    return matched, original_only, dynamic_only


def build_decision_matrix(matched, original_only, dynamic_only):
    rows = []
    if not matched.empty:
        grouped = matched.groupby(
            ["original_outcome", "dynamic_outcome"], dropna=False
        )
        for (original_outcome, dynamic_outcome), group in grouped:
            rows.append({
                "original_decision": original_outcome,
                "dynamic_decision": dynamic_outcome,
                "trades": len(group),
                "original_pnl": group["original_pnl"].sum(),
                "dynamic_pnl": group["dynamic_pnl"].sum(),
                "delta_pnl": group["delta_pnl"].sum(),
            })
    for outcome, group in original_only.groupby("comparison_outcome"):
        rows.append({
            "original_decision": outcome,
            "dynamic_decision": "NO_TRADE",
            "trades": len(group),
            "original_pnl": group["pnl"].sum(),
            "dynamic_pnl": 0.0,
            "delta_pnl": -group["pnl"].sum(),
        })
    for outcome, group in dynamic_only.groupby("comparison_outcome"):
        rows.append({
            "original_decision": "NO_TRADE",
            "dynamic_decision": outcome,
            "trades": len(group),
            "original_pnl": 0.0,
            "dynamic_pnl": group["pnl"].sum(),
            "delta_pnl": group["pnl"].sum(),
        })
    return pd.DataFrame(rows)


def build_filter_value_report(original_only, dynamic_only):
    sl_avoided = original_only[
        original_only["comparison_outcome"].eq("SL")
    ]
    tp_sacrificed = original_only[
        original_only["comparison_outcome"].eq("TP")
    ]
    rejected = len(sl_avoided) + len(tp_sacrificed)
    return {
        "sl_avoided": len(sl_avoided),
        "tp_sacrificed": len(tp_sacrificed),
        "avoidance_precision": (
            len(sl_avoided) / rejected * 100 if rejected else np.nan
        ),
        "avoided_loss_value": -float(sl_avoided["pnl"].sum()),
        "sacrificed_profit_value": float(tp_sacrificed["pnl"].sum()),
        "net_filter_value": -float(original_only["pnl"].sum()),
        "dynamic_only_pnl": float(dynamic_only["pnl"].sum()),
        "combined_incremental_value": (
            -float(original_only["pnl"].sum())
            + float(dynamic_only["pnl"].sum())
        ),
    }


def parse_candidate_rows(dynamic_df):
    rows = []
    for _, trade in dynamic_df.iterrows():
        raw = trade.get("comparison_candidates_json", "")
        try:
            candidates = json.loads(raw) if raw else []
        except (TypeError, ValueError, json.JSONDecodeError):
            candidates = []
        if not isinstance(candidates, list):
            continue
        candidate_10 = next(
            (
                item for item in candidates
                if int(item.get("lookback", -1)) == 10
            ),
            None,
        )
        selected_lookback = trade.get("comparison_selected_lookback")
        selected = next(
            (
                item for item in candidates
                if int(item.get("lookback", -1)) == selected_lookback
            ),
            None,
        )
        rows.append({
            "comparison_trade_key": trade["comparison_trade_key"],
            "symbol": trade["symbol"],
            "entry_ts": trade["comparison_entry_ts"],
            "outcome": trade["comparison_outcome"],
            "pnl": trade["pnl"],
            "selected_lookback": selected_lookback,
            "lookback_10_valid": (
                bool(candidate_10.get("is_compression"))
                if candidate_10 else False
            ),
            "lookback_10_score": (
                candidate_10.get("selection_score")
                if candidate_10 else np.nan
            ),
            "selected_score": (
                selected.get("selection_score")
                if selected else np.nan
            ),
        })
    result = pd.DataFrame(rows)
    if not result.empty:
        result["selection_margin_vs_10"] = (
            pd.to_numeric(result["selected_score"], errors="coerce")
            - pd.to_numeric(result["lookback_10_score"], errors="coerce")
        )
        result["selection_type"] = np.select(
            [
                result["selected_lookback"].eq(10),
                result["lookback_10_valid"],
            ],
            ["LOOKBACK_10", "LONG_REPLACED_VALID_10"],
            default="LONG_DISCOVERY",
        )
    return result


def build_lookback_report(dynamic_df):
    if dynamic_df.empty:
        return pd.DataFrame()
    rows = []
    total_trades = len(dynamic_df)
    for lookback, group in dynamic_df.groupby(
        "comparison_selected_lookback", dropna=False
    ):
        rows.append({
            "selected_lookback": lookback,
            "coverage_pct": (
                len(group) / total_trades * 100
                if total_trades else 0.0
            ),
            **calculate_strategy_summary(group),
        })
    return pd.DataFrame(rows).sort_values("selected_lookback")


def build_daily_report(original_df, dynamic_df):
    rows = []
    dates = sorted(
        set(original_df["comparison_date"].dropna())
        | set(dynamic_df["comparison_date"].dropna())
    )
    for date in dates:
        original_metrics = calculate_strategy_summary(
            original_df[original_df["comparison_date"].eq(date)]
        )
        dynamic_metrics = calculate_strategy_summary(
            dynamic_df[dynamic_df["comparison_date"].eq(date)]
        )
        rows.append({
            "date": date,
            "original_trades": original_metrics["trades"],
            "dynamic_trades": dynamic_metrics["trades"],
            "original_winrate": original_metrics["winrate"],
            "dynamic_winrate": dynamic_metrics["winrate"],
            "original_pf": original_metrics["profit_factor"],
            "dynamic_pf": dynamic_metrics["profit_factor"],
            "original_pnl": original_metrics["total_pnl"],
            "dynamic_pnl": dynamic_metrics["total_pnl"],
            "delta_pnl": dynamic_metrics["total_pnl"] - original_metrics["total_pnl"],
            "original_fees": original_metrics["fees"],
            "dynamic_fees": dynamic_metrics["fees"],
            "original_max_dd": original_metrics["max_drawdown"],
            "dynamic_max_dd": dynamic_metrics["max_drawdown"],
        })
    return pd.DataFrame(rows)


def build_robustness_report(df, strategy):
    if df.empty:
        return pd.DataFrame()
    scenarios = [("Full sample", df)]
    ordered = df.sort_values("comparison_entry_ts")
    midpoint = len(ordered) // 2
    if midpoint:
        scenarios.extend([
            ("First half", ordered.iloc[:midpoint]),
            ("Second half", ordered.iloc[midpoint:]),
        ])
    pnl_symbol = df.groupby("symbol")["pnl"].sum()
    if not pnl_symbol.empty:
        best_symbol = pnl_symbol.idxmax()
        scenarios.append((
            f"Without best symbol: {best_symbol}",
            df[~df["symbol"].eq(best_symbol)],
        ))
    pnl_day = df.groupby("comparison_date")["pnl"].sum()
    if not pnl_day.empty:
        best_day = pnl_day.idxmax()
        scenarios.append((
            f"Without best day: {best_day}",
            df[~df["comparison_date"].eq(best_day)],
        ))
    return pd.DataFrame([
        {"strategy": strategy, "scenario": name, **calculate_strategy_summary(part)}
        for name, part in scenarios
    ])


def build_overlap_report(original_df, dynamic_df, matched):
    shared_symbols = set(original_df["symbol"]) & set(dynamic_df["symbol"])
    temporal_overlaps = 0
    same_symbol_overlaps = 0
    same_batch_overlaps = 0
    for _, original in original_df.iterrows():
        original_exit = original["comparison_exit_ts"]
        if pd.isna(original_exit):
            original_exit = original["comparison_entry_ts"]
        for _, dynamic in dynamic_df.iterrows():
            dynamic_exit = dynamic["comparison_exit_ts"]
            if pd.isna(dynamic_exit):
                dynamic_exit = dynamic["comparison_entry_ts"]
            overlaps = (
                original["comparison_entry_ts"] <= dynamic_exit
                and dynamic["comparison_entry_ts"] <= original_exit
            )
            if overlaps:
                temporal_overlaps += 1
                if original["symbol"] == dynamic["symbol"]:
                    same_symbol_overlaps += 1
            entry_distance = abs(
                original["comparison_entry_ts"]
                - dynamic["comparison_entry_ts"]
            )
            if entry_distance <= pd.Timedelta(minutes=30):
                same_batch_overlaps += 1

    matched_pnl_correlation = np.nan
    if len(matched) >= 3:
        matched_pnl_correlation = matched[
            ["original_pnl", "dynamic_pnl"]
        ].corr().iloc[0, 1]

    events = []
    for frame in (original_df, dynamic_df):
        for _, trade in frame.iterrows():
            start = trade["comparison_entry_ts"]
            end = trade["comparison_exit_ts"]
            if pd.isna(start):
                continue
            if pd.isna(end) or end < start:
                end = start
            notional = trade.get("comparison_notional", np.nan)
            notional = float(notional) if pd.notna(notional) else 0.0
            events.append((start, 1, notional))
            events.append((end, -1, -notional))
    events.sort(key=lambda item: (item[0], item[1]))
    active_positions = 0
    active_notional = 0.0
    max_combined_positions = 0
    max_combined_notional = 0.0
    for _, position_delta, notional_delta in events:
        active_positions += position_delta
        active_notional += notional_delta
        max_combined_positions = max(
            max_combined_positions, active_positions
        )
        max_combined_notional = max(
            max_combined_notional, active_notional
        )

    return {
        "matched_opportunities": len(matched),
        "shared_symbols": len(shared_symbols),
        "original_match_rate": (
            len(matched) / len(original_df) * 100 if len(original_df) else np.nan
        ),
        "dynamic_match_rate": (
            len(matched) / len(dynamic_df) * 100 if len(dynamic_df) else np.nan
        ),
        "avg_entry_delta_minutes": (
            matched["entry_delta_minutes"].mean()
            if not matched.empty else np.nan
        ),
        "temporal_position_overlaps": temporal_overlaps,
        "same_symbol_position_overlaps": same_symbol_overlaps,
        "same_30m_batch_entries": same_batch_overlaps,
        "matched_pnl_correlation": matched_pnl_correlation,
        "max_combined_positions": max_combined_positions,
        "max_combined_notional": max_combined_notional,
    }
