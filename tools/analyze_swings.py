import pandas as pd
import numpy as np

CSV_PATH = "trades.csv"

# =========================
# LOAD
# =========================

df = pd.read_csv(CSV_PATH)

df["pnl"] = pd.to_numeric(df["pnl"], errors="coerce")
df["max_favorable_pct"] = pd.to_numeric(df["max_favorable_pct"], errors="coerce")
df["max_adverse_pct"] = pd.to_numeric(df["max_adverse_pct"], errors="coerce")

df = df.dropna(subset=["pnl"])

# =========================
# HELPERS
# =========================

def to_bool(series):
    return (
        series.astype(str)
        .str.lower()
        .isin(["true", "1", "yes"])
    )

def stats(name, x):
    if len(x) == 0:
        return None

    wins = (x["pnl"] > 0).sum()
    gross_profit = x.loc[x["pnl"] > 0, "pnl"].sum()
    gross_loss = abs(x.loc[x["pnl"] < 0, "pnl"].sum())

    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 0
        else np.inf
    )

    return {
        "setup": name,
        "trades": len(x),
        "winrate": round(wins / len(x) * 100, 2),
        "avg_return": round(x["pnl"].mean(), 4),
        "avg_mfe": round(x["max_favorable_pct"].mean(), 4),
        "avg_mae": round(x["max_adverse_pct"].mean(), 4),
        "profit_factor": round(profit_factor, 2) if np.isfinite(profit_factor) else np.inf,
    }

# =========================
# NEAR SWING STATS
# =========================

near_results = []

for tf in ["15m", "1h", "4h"]:
    for side in ["LONG", "SHORT"]:
        for ref in ["low", "high"]:
            col = f"near_swing_{ref}_{tf}"

            if col not in df.columns:
                continue

            mask = (
                (df["side"] == side)
                & to_bool(df[col])
            )

            row = stats(
                f"{side} near swing {ref} {tf}",
                df[mask]
            )

            if row:
                near_results.append(row)

near_df = pd.DataFrame(near_results)

print("\n========== NEAR SWING STATS ==========\n")

if not near_df.empty:
    print(
        near_df.sort_values(
            ["profit_factor", "trades"],
            ascending=[False, False]
        ).to_string(index=False)
    )
else:
    print("No near swing data found.")

# =========================
# DISTANCE BUCKET STATS
# =========================

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

def bucket_stats(side, tf, ref):
    col = f"dist_swing_{ref}_{tf}_pct"

    if col not in df.columns:
        return

    temp = df[df["side"] == side].copy()
    temp[col] = pd.to_numeric(temp[col], errors="coerce")
    temp = temp.dropna(subset=[col, "pnl"])

    if temp.empty:
        return

    temp["bucket"] = pd.cut(
        temp[col],
        bins=BUCKETS,
        labels=LABELS,
        include_lowest=True
    )

    for bucket, group in temp.groupby("bucket", observed=False):
        if len(group) == 0:
            continue

        row = stats(
            f"{side} dist swing {ref} {tf} {bucket}",
            group
        )

        if row:
            row["side"] = side
            row["tf"] = tf
            row["reference"] = ref
            row["bucket"] = str(bucket)
            distance_results.append(row)

for tf in ["15m", "1h", "4h"]:
    for side in ["LONG", "SHORT"]:
        for ref in ["low", "high"]:
            bucket_stats(side, tf, ref)

distance_df = pd.DataFrame(distance_results)

print("\n========== DISTANCE BUCKET STATS ==========\n")

if not distance_df.empty:
    print(
        distance_df.sort_values(
            ["profit_factor", "trades"],
            ascending=[False, False]
        ).to_string(index=False)
    )
else:
    print("No distance bucket data found.")

# =========================
# DISTANCE BUCKET MIN TRADES
# =========================

MIN_TRADES = 10

print(f"\n========== DISTANCE BUCKET STATS MIN {MIN_TRADES} TRADES ==========\n")

if not distance_df.empty:
    filtered = distance_df[distance_df["trades"] >= MIN_TRADES]

    if not filtered.empty:
        print(
            filtered.sort_values(
                ["profit_factor", "trades"],
                ascending=[False, False]
            ).to_string(index=False)
        )
    else:
        print(f"No buckets with at least {MIN_TRADES} trades.")
else:
    print("No distance bucket data found.")

# =========================
# WORST BUCKETS MIN TRADES
# =========================

print(f"\n========== WORST BUCKETS MIN {MIN_TRADES} TRADES ==========\n")

if not distance_df.empty:
    filtered = distance_df[distance_df["trades"] >= MIN_TRADES]

    if not filtered.empty:
        print(
            filtered.sort_values(
                ["profit_factor", "avg_return"],
                ascending=[True, True]
            ).to_string(index=False)
        )
    else:
        print(f"No buckets with at least {MIN_TRADES} trades.")
        
print("\n========== ROUTER x SWING ==========\n")

router_results = []

for reason in df["router_reason"].dropna().unique():

    for side in ["LONG", "SHORT"]:

        for tf in ["15m", "1h", "4h"]:

            for ref in ["low", "high"]:

                col = f"dist_swing_{ref}_{tf}_pct"

                if col not in df.columns:
                    continue

                temp = df[
                    (df["router_reason"] == reason)
                    & (df["side"] == side)
                ].copy()

                temp[col] = pd.to_numeric(temp[col], errors="coerce")
                temp = temp.dropna(subset=[col])

                if len(temp) == 0:
                    continue

                temp["bucket"] = pd.cut(
                    temp[col],
                    bins=BUCKETS,
                    labels=LABELS,
                    include_lowest=True
                )

                for bucket, group in temp.groupby("bucket", observed=False):

                    if len(group) < 10:
                        continue

                    row = stats(
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

print(
    router_df.sort_values(
        ["profit_factor", "trades"],
        ascending=[False, False]
    ).to_string(index=False)
)