# tools/discover_btc_patterns.py

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


DEFAULT_INPUT = "reports/btc_context_days.csv"
DEFAULT_OUTPUT_DIR = "reports/btc_patterns"


def parse_days(value: str | None) -> list[str]:
    if not value:
        return []

    return [
        d.strip()
        for d in value.split(",")
        if d.strip()
    ]


def safe_numeric_df(df: pd.DataFrame) -> pd.DataFrame:
    exclude_cols = ["date", "btc_context_regime_auto"]

    numeric_cols = []

    for col in df.columns:
        if col in exclude_cols:
            continue

        converted = pd.to_numeric(df[col], errors="coerce")

        if converted.notna().sum() > 0:
            numeric_cols.append(col)

    return df[numeric_cols].apply(pd.to_numeric, errors="coerce")


def compare_numeric_features(
    df: pd.DataFrame,
    target_days: list[str],
    label: str,
    top_n: int = 40,
) -> pd.DataFrame:
    target = df[df["date"].isin(target_days)].copy()
    rest = df[~df["date"].isin(target_days)].copy()

    if target.empty:
        print(f"[WARN] No rows found for {label}")
        return pd.DataFrame()

    num_target = safe_numeric_df(target)
    num_rest = safe_numeric_df(rest)

    rows = []

    for col in num_target.columns:
        target_mean = num_target[col].mean()
        rest_mean = num_rest[col].mean()
        target_median = num_target[col].median()
        rest_median = num_rest[col].median()

        if pd.isna(target_mean) or pd.isna(rest_mean):
            continue

        diff = target_mean - rest_mean

        rest_std = num_rest[col].std()
        z_like = diff / rest_std if rest_std and not pd.isna(rest_std) and rest_std != 0 else np.nan

        rows.append({
            "group": label,
            "feature": col,
            "target_mean": target_mean,
            "rest_mean": rest_mean,
            "diff": diff,
            "abs_diff": abs(diff),
            "target_median": target_median,
            "rest_median": rest_median,
            "z_like": z_like,
            "abs_z_like": abs(z_like) if not pd.isna(z_like) else np.nan,
        })

    out = pd.DataFrame(rows)

    if out.empty:
        return out

    return (
        out.sort_values(["abs_z_like", "abs_diff"], ascending=False)
        .head(top_n)
        .reset_index(drop=True)
    )


def analyze_signature_columns(
    df: pd.DataFrame,
    target_days: list[str],
    label: str,
) -> pd.DataFrame:
    target = df[df["date"].isin(target_days)].copy()
    rest = df[~df["date"].isin(target_days)].copy()

    signature_cols = [
        c for c in df.columns
        if (
            c.endswith("dominant_trend_signature")
            or c.endswith("dominant_compression_signature")
            or c.endswith("_regime_auto")
        )
        and not c.endswith("_pct")
    ]

    rows = []

    for col in signature_cols:
        target_counts = target[col].astype(str).replace("nan", "").value_counts(normalize=True)
        rest_counts = rest[col].astype(str).replace("nan", "").value_counts(normalize=True)

        values = set(target_counts.index).union(set(rest_counts.index))

        for value in values:
            if value == "":
                continue

            target_pct = target_counts.get(value, 0) * 100
            rest_pct = rest_counts.get(value, 0) * 100

            rows.append({
                "group": label,
                "feature": col,
                "value": value,
                "target_pct": target_pct,
                "rest_pct": rest_pct,
                "diff_pct": target_pct - rest_pct,
                "abs_diff_pct": abs(target_pct - rest_pct),
            })

    out = pd.DataFrame(rows)

    if out.empty:
        return out

    return (
        out.sort_values("abs_diff_pct", ascending=False)
        .reset_index(drop=True)
    )


def summarize_days(df: pd.DataFrame, days: list[str], label: str):
    subset = df[df["date"].isin(days)].copy()

    print("")
    print("=" * 80)
    print(label)
    print("=" * 80)

    if subset.empty:
        print("No matching days found.")
        return

    print(f"Days found: {len(subset)}")
    print(", ".join(subset["date"].astype(str).tolist()))

    bot_cols = [
        "bot_trades",
        "bot_winrate",
        "bot_profit_factor",
        "bot_net",
        "bot_expectancy",
    ]

    existing_bot_cols = [c for c in bot_cols if c in subset.columns]

    if existing_bot_cols:
        print("")
        print("Bot metrics:")
        for col in existing_bot_cols:
            print(f"- {col}: {pd.to_numeric(subset[col], errors='coerce').mean():.4f}")


def print_top(title: str, df: pd.DataFrame, n: int = 15):
    print("")
    print("-" * 80)
    print(title)
    print("-" * 80)

    if df.empty:
        print("No data.")
        return

    print(df.head(n).to_string(index=False))
    
def build_candidate_rules(
    signatures_df: pd.DataFrame,
    label: str,
    min_target_pct: float = 60,
    min_edge: float = 25,
    top_n: int = 30,
) -> pd.DataFrame:
    if signatures_df.empty:
        return pd.DataFrame()

    rules = signatures_df[
        (signatures_df["target_pct"] >= min_target_pct) &
        (signatures_df["diff_pct"] >= min_edge)
    ].copy()

    if rules.empty:
        return pd.DataFrame()

    rules = rules.sort_values("diff_pct", ascending=False).head(top_n)

    rules["rule"] = (
        "IF "
        + rules["feature"].astype(str)
        + " == "
        + rules["value"].astype(str)
        + " THEN "
        + label
    )

    rules = rules.rename(columns={
        "target_pct": "target_coverage_pct",
        "rest_pct": "rest_coverage_pct",
        "diff_pct": "edge_pct",
    })

    rules["rule_score"] = (
    rules["target_coverage_pct"] * 0.45
    + rules["edge_pct"] * 0.45
    - rules["rest_coverage_pct"] * 0.10
    )

    return rules[
        [
            "group",
            "rule",
            "feature",
            "value",
            "target_coverage_pct",
            "rest_coverage_pct",
            "edge_pct",
            "rule_score",
            "abs_diff_pct",
        ]
    ].reset_index(drop=True)


def print_candidate_rules(rules_df: pd.DataFrame, label: str):
    print("")
    print("=" * 80)
    print(f"{label} - CANDIDATE RULES")
    print("=" * 80)

    if rules_df.empty:
        print("No strong candidate rules.")
        return

    for i, row in enumerate(rules_df.itertuples(index=False), start=1):
        print(f"\n#{i}")
        print(row.rule)
        print(f"Target coverage: {row.target_coverage_pct:.2f}%")
        print(f"Rest coverage:   {row.rest_coverage_pct:.2f}%")
        print(f"Edge:            +{row.edge_pct:.2f}%")
        print(f"Rule score:      {row.rule_score:.2f}")
        
def print_context_summary(label: str, rules_df: pd.DataFrame):
    print("")
    print("=" * 80)
    print(f"{label} - CONTEXT SUMMARY")
    print("=" * 80)

    if rules_df.empty:
        print("No context summary available.")
        return

    top = rules_df.sort_values("rule_score", ascending=False).head(8)

    for row in top.itertuples(index=False):
        print(f"- {row.feature} == {row.value}")
        print(
            f"  coverage={row.target_coverage_pct:.1f}% | "
            f"rest={row.rest_coverage_pct:.1f}% | "
            f"edge={row.edge_pct:.1f}% | "
            f"score={row.rule_score:.1f}"
        )
        
def build_text_summary(candidate_rule_reports: list[pd.DataFrame]) -> str:
    lines = []
    lines.append("BTC Pattern Discovery Summary")
    lines.append("=" * 80)

    if not candidate_rule_reports:
        lines.append("No candidate rules.")
        return "\n".join(lines)

    rules = pd.concat(candidate_rule_reports, ignore_index=True)

    if rules.empty:
        lines.append("No candidate rules.")
        return "\n".join(lines)

    for group, g in rules.groupby("group"):
        lines.append("")
        lines.append(f"{group} TOP RULES")
        lines.append("-" * 80)

        top = g.sort_values("rule_score", ascending=False).head(10)

        for row in top.itertuples(index=False):
            lines.append(row.rule)
            lines.append(
                f"coverage={row.target_coverage_pct:.2f}% | "
                f"rest={row.rest_coverage_pct:.2f}% | "
                f"edge={row.edge_pct:.2f}% | "
                f"score={row.rule_score:.2f}"
            )
            lines.append("")

    return "\n".join(lines)

def build_daily_context_profile(df: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "date",

        "bot_trades",
        "bot_winrate",
        "bot_profit_factor",
        "bot_net",
        "bot_expectancy",
        "bot_tp_count",
        "bot_sl_count",
        "bot_max_tp_streak",
        "bot_max_sl_streak",
        "bot_tp_rate",
        "bot_sl_rate",

        "btc_context_regime_auto",

        "btc_15m_dominant_trend_signature",
        "btc_30m_dominant_trend_signature",
        "btc_1h_dominant_trend_signature",
        "btc_4h_dominant_trend_signature",

        "btc_15m_dominant_trend_signature_pct",
        "btc_30m_dominant_trend_signature_pct",
        "btc_1h_dominant_trend_signature_pct",
        "btc_4h_dominant_trend_signature_pct",

        "btc_15m_dominant_compression_signature",
        "btc_30m_dominant_compression_signature",
        "btc_1h_dominant_compression_signature",
        "btc_4h_dominant_compression_signature",

        "btc_15m_dominant_compression_signature_pct",
        "btc_30m_dominant_compression_signature_pct",
        "btc_1h_dominant_compression_signature_pct",
        "btc_4h_dominant_compression_signature_pct",

        "btc_15m_real_trend_up_pct",
        "btc_30m_real_trend_up_pct",
        "btc_1h_real_trend_up_pct",
        "btc_4h_real_trend_up_pct",

        "btc_15m_real_time_in_compression_pct",
        "btc_30m_real_time_in_compression_pct",
        "btc_1h_real_time_in_compression_pct",
        "btc_4h_real_time_in_compression_pct",

        "btc_15m_breakout_count",
        "btc_30m_breakout_count",
        "btc_1h_breakout_count",
        "btc_4h_breakout_count",

        "btc_15m_efficiency_ratio",
        "btc_30m_efficiency_ratio",
        "btc_1h_efficiency_ratio",
        "btc_4h_efficiency_ratio",
    ]

    existing_cols = [c for c in cols if c in df.columns]

    profile = df[existing_cols].copy()

    return profile.sort_values("date").reset_index(drop=True)

def max_streak_values(values, target_value: str) -> int:
    best = 0
    current = 0

    for v in values:
        if str(v).upper() == target_value:
            current += 1
            best = max(best, current)
        else:
            current = 0

    return best


def load_trades_daily_metrics(path: str) -> pd.DataFrame:
    if not path:
        return pd.DataFrame()

    trades_path = Path(path)

    if not trades_path.exists():
        print(f"[WARN] trades file not found: {trades_path}")
        return pd.DataFrame()

    trades = pd.read_csv(trades_path)

    if "entry_ts" not in trades.columns:
        print("[WARN] trades file has no entry_ts column")
        return pd.DataFrame()

    trades["entry_dt"] = pd.to_datetime(
        trades["entry_ts"],
        utc=True,
        errors="coerce",
    )

    trades = trades.dropna(subset=["entry_dt"]).copy()
    trades["date"] = trades["entry_dt"].dt.date.astype(str)

    pnl_col = "pnl" if "pnl" in trades.columns else None
    exit_col = "exit_reason" if "exit_reason" in trades.columns else None

    rows = []

    for date, g in trades.groupby("date"):
        g = g.sort_values("entry_dt").copy()

        row = {"date": date}
        row["bot_trades"] = len(g)

        if pnl_col:
            pnl = pd.to_numeric(g[pnl_col], errors="coerce").fillna(0)

            wins = pnl[pnl > 0]
            losses = pnl[pnl < 0]

            row["bot_net"] = pnl.sum()
            row["bot_winrate"] = len(wins) / len(pnl) * 100 if len(pnl) else 0
            row["bot_avg_win"] = wins.mean() if len(wins) else 0
            row["bot_avg_loss"] = losses.mean() if len(losses) else 0
            row["bot_expectancy"] = pnl.mean() if len(pnl) else 0

            gross_win = wins.sum()
            gross_loss = abs(losses.sum())

            row["bot_profit_factor"] = (
                gross_win / gross_loss
                if gross_loss > 0
                else 0
            )

        if exit_col:
            exits = g[exit_col].astype(str).str.upper()

            row["bot_tp_count"] = int((exits == "TP").sum())
            row["bot_sl_count"] = int((exits == "SL").sum())
            row["bot_max_tp_streak"] = max_streak_values(exits, "TP")
            row["bot_max_sl_streak"] = max_streak_values(exits, "SL")
            row["bot_sl_rate"] = row["bot_sl_count"] / len(g) * 100 if len(g) else 0
            row["bot_tp_rate"] = row["bot_tp_count"] / len(g) * 100 if len(g) else 0

        rows.append(row)

    return pd.DataFrame(rows)


def merge_trades_metrics(context_df: pd.DataFrame, trades_daily: pd.DataFrame) -> pd.DataFrame:
    if trades_daily.empty:
        return context_df

    df = context_df.copy()

    override_cols = [
        c for c in trades_daily.columns
        if c != "date"
    ]

    df = df.drop(columns=[c for c in override_cols if c in df.columns], errors="ignore")

    df = df.merge(
        trades_daily,
        on="date",
        how="left",
    )

    for col in override_cols:
        df[col] = df[col].fillna(0)

    return df
   
def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--input", type=str, default=DEFAULT_INPUT)
    parser.add_argument("--trades", type=str, default="")
    parser.add_argument("--output-dir", type=str, default=DEFAULT_OUTPUT_DIR)

    parser.add_argument("--good-days", type=str, default="")
    parser.add_argument("--bad-days", type=str, default="")
    parser.add_argument("--choppy-days", type=str, default="")
    parser.add_argument("--top-n", type=int, default=40)

    args = parser.parse_args()

    input_path = Path(args.input)

    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    df = pd.read_csv(input_path)
    df["date"] = df["date"].astype(str)
    
    trades_daily = load_trades_daily_metrics(args.trades)
    df = merge_trades_metrics(df, trades_daily)

    good_days = parse_days(args.good_days)
    bad_days = parse_days(args.bad_days)
    choppy_days = parse_days(args.choppy_days)

    numeric_reports = []
    signature_reports = []
    candidate_rule_reports = []

    if good_days:
        summarize_days(df, good_days, "GOOD DAYS")

        good_numeric = compare_numeric_features(
            df,
            good_days,
            label="GOOD",
            top_n=args.top_n,
        )

        good_signatures = analyze_signature_columns(
            df,
            good_days,
            label="GOOD",
        )

        print_top("GOOD DAYS - Top numeric differences", good_numeric)
        print_top("GOOD DAYS - Top signature differences", good_signatures)

        good_rules = build_candidate_rules(good_signatures, "GOOD")
        print_candidate_rules(good_rules, "GOOD")
        print_context_summary("GOOD", good_rules)

        numeric_reports.append(good_numeric)
        signature_reports.append(good_signatures)
        candidate_rule_reports.append(good_rules)

    if bad_days:
        summarize_days(df, bad_days, "BAD DAYS")

        bad_numeric = compare_numeric_features(
            df,
            bad_days,
            label="BAD",
            top_n=args.top_n,
        )

        bad_signatures = analyze_signature_columns(
            df,
            bad_days,
            label="BAD",
        )

        print_top("BAD DAYS - Top numeric differences", bad_numeric)
        print_top("BAD DAYS - Top signature differences", bad_signatures)

        bad_rules = build_candidate_rules(bad_signatures, "BAD")
        print_candidate_rules(bad_rules, "BAD")
        print_context_summary("BAD", bad_rules)

        numeric_reports.append(bad_numeric)
        signature_reports.append(bad_signatures)
        candidate_rule_reports.append(bad_rules)
        
    if choppy_days:
        summarize_days(df, choppy_days, "CHOPPY DAYS")

        choppy_numeric = compare_numeric_features(
            df,
            choppy_days,
            label="CHOPPY",
            top_n=args.top_n,
        )

        choppy_signatures = analyze_signature_columns(
            df,
            choppy_days,
            label="CHOPPY",
        )

        print_top("CHOPPY DAYS - Top numeric differences", choppy_numeric)
        print_top("CHOPPY DAYS - Top signature differences", choppy_signatures)

        choppy_rules = build_candidate_rules(choppy_signatures, "CHOPPY")
        print_candidate_rules(choppy_rules, "CHOPPY")
        print_context_summary("CHOPPY", choppy_rules)

        numeric_reports.append(choppy_numeric)
        signature_reports.append(choppy_signatures)
        candidate_rule_reports.append(choppy_rules)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    exported = []

    if numeric_reports:
        numeric_df = pd.concat(numeric_reports, ignore_index=True)
        path = output_dir / "numeric_differences.csv"
        numeric_df.to_csv(path, index=False)
        exported.append(path)

    if signature_reports:
        signature_df = pd.concat(signature_reports, ignore_index=True)
        path = output_dir / "signature_differences.csv"
        signature_df.to_csv(path, index=False)
        exported.append(path)

    if candidate_rule_reports:
        rules_df = (
            pd.concat(candidate_rule_reports, ignore_index=True)
            .sort_values("rule_score", ascending=False)
            .reset_index(drop=True)
        )
        
        path = output_dir / "candidate_rules.csv"
        rules_df.to_csv(path, index=False)
        exported.append(path)
        
    summary_path = output_dir / "summary.txt"
    summary_path.write_text(
        build_text_summary(candidate_rule_reports),
        encoding="utf-8"
    )
    exported.append(summary_path)
    
    daily_profile = build_daily_context_profile(df)
    daily_profile_path = output_dir / "daily_context_profile.csv"
    daily_profile.to_csv(daily_profile_path, index=False)
    exported.append(daily_profile_path)

    if exported:
        print("")
        print("=" * 80)
        print("Pattern files exported:")
        for path in exported:
            print(path)
        print("=" * 80)
    else:
        print("No good-days, bad-days or choppy-days provided.")


if __name__ == "__main__":
    main()