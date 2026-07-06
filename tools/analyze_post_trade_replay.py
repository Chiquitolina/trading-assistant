import argparse
import time
from pathlib import Path

import ccxt
import numpy as np
import pandas as pd


BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_TRADES_FILE = BASE_DIR / "trades.csv"
DEFAULT_REPORT_FILE = BASE_DIR / "reports" / "post_trade_replay.csv"
DEFAULT_SCENARIOS_FILE = BASE_DIR / "reports" / "tp_sl_scenarios.csv"

TIMEFRAME = "1m"
MS_PER_CANDLE = 60_000
FETCH_LIMIT = 1000

TP_TARGETS_PCT = [
    0.30,
    0.50,
    0.75,
    1.00,
    1.50,
    2.00,
    2.50,
    6.00,
    10.00,
]

STRUCTURAL_SL_BUFFERS_PCT = [0.00, 0.10, 0.20]

PARTIAL_TARGETS_PCT = [
    1.00,
    2.50,
    6.00,
    10.00,
]

PARTIAL_STRATEGIES = {
    "conservative": [
        0.50,
        0.30,
        0.15,
        0.05,
    ],
    "balanced": [
        0.40,
        0.30,
        0.20,
        0.10,
    ],
    "runner_aggressive": [
        0.25,
        0.25,
        0.25,
        0.25,
    ],
}

PARTIAL_SL_POLICIES = [
    "HOLD_STRUCTURAL",
    "BREAK_EVEN_AFTER_TP1",
    "STEP_TRAIL",
]

DEFAULT_PARTIALS_FILE = (
    BASE_DIR
    / "reports"
    / "partial_tp_scenarios.csv"
)

exchange = ccxt.binanceusdm({
    "enableRateLimit": True,
})

def parse_timestamp(value):
    if pd.isna(value):
        return pd.NaT

    try:
        numeric = float(value)

        unit = "ms" if numeric > 10_000_000_000 else "s"

        return pd.to_datetime(
            numeric,
            unit=unit,
            utc=True,
            errors="coerce",
        )

    except (TypeError, ValueError):
        return pd.to_datetime(
            value,
            utc=True,
            errors="coerce",
        )

def fetch_history_range(
    symbol: str,
    since_ts,
    until_ts,
    price_type="trade",
) -> pd.DataFrame:
    since = parse_timestamp(since_ts)
    until = parse_timestamp(until_ts)

    if pd.isna(since) or pd.isna(until):
        return pd.DataFrame()

    since_ms = int(since.timestamp() * 1000)
    until_ms = int(until.timestamp() * 1000)

    rows = []

    while since_ms <= until_ms:
        params = {}

        if price_type == "mark":
            params["price"] = "mark"

        candles = exchange.fetch_ohlcv(
            symbol,
            timeframe=TIMEFRAME,
            since=since_ms,
            limit=FETCH_LIMIT,
            params=params,
        )

        if not candles:
            break

        valid = [
            candle
            for candle in candles
            if candle[0] <= until_ms
        ]

        rows.extend(valid)

        last_ts = candles[-1][0]
        next_since = last_ts + MS_PER_CANDLE

        if next_since <= since_ms:
            break

        since_ms = next_since

        if len(candles) < FETCH_LIMIT or last_ts >= until_ms:
            break

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(
        rows,
        columns=[
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ],
    )

    df["timestamp"] = pd.to_datetime(
        df["timestamp"],
        unit="ms",
        utc=True,
    )

    return (
        df
        .drop_duplicates(subset=["timestamp"])
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    
def fetch_replay_history(
    symbol,
    since_ts,
    until_ts,
):
    trade_candles = fetch_history_range(
        symbol=symbol,
        since_ts=since_ts,
        until_ts=until_ts,
        price_type="trade",
    )

    mark_candles = fetch_history_range(
        symbol=symbol,
        since_ts=since_ts,
        until_ts=until_ts,
        price_type="mark",
    )

    if trade_candles.empty or mark_candles.empty:
        return pd.DataFrame()

    mark_view = mark_candles[
        [
            "timestamp",
            "open",
            "high",
            "low",
            "close",
        ]
    ].rename(
        columns={
            "open": "mark_open",
            "high": "mark_high",
            "low": "mark_low",
            "close": "mark_close",
        }
    )

    combined = trade_candles.merge(
        mark_view,
        on="timestamp",
        how="inner",
        validate="one_to_one",
    )

    return (
        combined
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    
def favorable_move_pct(price, entry, side):
    if side == "LONG":
        return ((price - entry) / entry) * 100

    return ((entry - price) / entry) * 100


def adverse_move_pct(price, entry, side):
    if side == "LONG":
        return ((entry - price) / entry) * 100

    return ((price - entry) / entry) * 100

def replay_scenario(
    candles: pd.DataFrame,
    tp_price: float,
    sl_price: float,
    side: str,
):
    side = str(side).upper()

    required_columns = {
        "timestamp",
        "high",
        "low",
        "mark_high",
        "mark_low",
    }

    if candles.empty:
        return {
            "result": "UNRESOLVED",
            "first_touch": None,
            "touch_ts": pd.NaT,
            "candles_elapsed": 0,
        }

    if not required_columns.issubset(candles.columns):
        return {
            "result": "MISSING_PRICE_DATA",
            "first_touch": None,
            "touch_ts": pd.NaT,
            "candles_elapsed": 0,
        }

    for candle_number, row in enumerate(
        candles.itertuples(index=False),
        start=1,
    ):
        if side == "LONG":
            # TP LIMIT: precio negociado
            tp_touched = row.high >= tp_price

            # SL STOP_MARKET workingType=MARK_PRICE
            sl_touched = row.mark_low <= sl_price

        else:
            # TP LIMIT: precio negociado
            tp_touched = row.low <= tp_price

            # SL STOP_MARKET workingType=MARK_PRICE
            sl_touched = row.mark_high >= sl_price

        if tp_touched and sl_touched:
            return {
                "result": "AMBIGUOUS",
                "first_touch": "same_1m_candle",
                "touch_ts": row.timestamp,
                "candles_elapsed": candle_number,
            }

        if tp_touched:
            return {
                "result": "TP",
                "first_touch": "tp_trade_price",
                "touch_ts": row.timestamp,
                "candles_elapsed": candle_number,
            }

        if sl_touched:
            return {
                "result": "SL",
                "first_touch": "sl_mark_price",
                "touch_ts": row.timestamp,
                "candles_elapsed": candle_number,
            }

    return {
        "result": "UNRESOLVED",
        "first_touch": None,
        "touch_ts": pd.NaT,
        "candles_elapsed": len(candles),
    }
    
def build_structural_sl(
    side,
    compression_low,
    compression_high,
    buffer_pct,
):
    side = str(side).upper()

    if side == "LONG":
        if pd.isna(compression_low):
            return np.nan

        return compression_low * (1 - buffer_pct / 100)

    if pd.isna(compression_high):
        return np.nan

    return compression_high * (1 + buffer_pct / 100)

def minutes_between(start, end):
    start = parse_timestamp(start)
    end = parse_timestamp(end)

    if pd.isna(start) or pd.isna(end):
        return np.nan

    return (end - start).total_seconds() / 60

def classify_exit_reason(value):
    reason = (
        str(value)
        .strip()
        .upper()
        .replace("-", "_")
        .replace(" ", "_")
    )

    sl_reasons = {
        "SL",
        "STOP",
        "STOP_LOSS",
        "STOPLOSS",
        "INITIAL_SL",
        "TRAILING_SL",
    }

    tp_reasons = {
        "TP",
        "TAKE_PROFIT",
        "TAKEPROFIT",
    }

    if reason in sl_reasons:
        return "SL"

    if reason in tp_reasons:
        return "TP"

    return reason

def price_from_target_pct(entry, target_pct, side):
    if side == "LONG":
        return entry * (1 + target_pct / 100)

    return entry * (1 - target_pct / 100)

def replay_partial_strategy(
    candles,
    entry,
    side,
    structural_sl,
    targets_pct,
    fractions,
    sl_policy,
    cost_pct=0.10,
):
    side = str(side).upper()

    if candles.empty or pd.isna(structural_sl):
        return {
            "result": "MISSING_DATA",
            "gross_pnl_pct": np.nan,
            "net_pnl_pct": np.nan,
        }

    if len(targets_pct) != len(fractions):
        raise ValueError(
            "targets_pct and fractions must have the same length"
        )

    if not np.isclose(sum(fractions), 1.0):
        raise ValueError(
            "partial fractions must add up to 1.0"
        )

    targets = [
        price_from_target_pct(
            entry,
            target_pct,
            side,
        )
        for target_pct in targets_pct
    ]

    remaining_fraction = 1.0
    current_sl = structural_sl
    realized_gross_pct = 0.0

    targets_hit = 0
    exit_events = 0
    final_ts = pd.NaT
    result = "UNRESOLVED"

    for row in candles.itertuples(index=False):
        if side == "LONG":
            sl_touched = row.mark_low <= current_sl
        else:
            sl_touched = row.mark_high >= current_sl

        next_target_touched = False

        if targets_hit < len(targets):
            next_target = targets[targets_hit]

            if side == "LONG":
                next_target_touched = row.high >= next_target
            else:
                next_target_touched = row.low <= next_target

        if sl_touched and next_target_touched:
            result = "AMBIGUOUS"
            final_ts = row.timestamp
            break

        if sl_touched:
            if side == "LONG":
                sl_return_pct = (
                    (current_sl - entry)
                    / entry
                ) * 100
            else:
                sl_return_pct = (
                    (entry - current_sl)
                    / entry
                ) * 100

            realized_gross_pct += (
                remaining_fraction
                * sl_return_pct
            )

            exit_events += 1
            remaining_fraction = 0.0
            final_ts = row.timestamp

            result = (
                "PARTIAL_THEN_SL"
                if targets_hit > 0
                else "FULL_SL"
            )

            break

        if next_target_touched:
            fraction_to_close = fractions[targets_hit]

            target_return_pct = targets_pct[targets_hit]

            realized_gross_pct += (
                fraction_to_close
                * target_return_pct
            )

            remaining_fraction -= fraction_to_close
            remaining_fraction = max(
                remaining_fraction,
                0.0,
            )

            targets_hit += 1
            exit_events += 1
            final_ts = row.timestamp

            if targets_hit == len(targets):
                result = "ALL_TARGETS"
                break

            if sl_policy == "BREAK_EVEN_AFTER_TP1":
                if targets_hit >= 1:
                    current_sl = entry

            elif sl_policy == "STEP_TRAIL":
                if targets_hit == 1:
                    current_sl = entry
                else:
                    previous_target_index = targets_hit - 2
                    current_sl = targets[
                        previous_target_index
                    ]

    if remaining_fraction > 0 and result == "UNRESOLVED":
        last_close = candles.iloc[-1]["close"]

        if side == "LONG":
            time_exit_return_pct = (
                (last_close - entry)
                / entry
            ) * 100
        else:
            time_exit_return_pct = (
                (entry - last_close)
                / entry
            ) * 100

        realized_gross_pct += (
            remaining_fraction
            * time_exit_return_pct
        )

        exit_events += 1
        remaining_fraction = 0.0
        final_ts = candles.iloc[-1]["timestamp"]
        result = "TIME_EXIT"

    # cost_pct representa coste total estimado sobre el 100%
    # de la posición, no coste completo por cada parcial.
    net_pnl_pct = realized_gross_pct - cost_pct

    return {
        "result": result,
        "targets_hit": targets_hit,
        "gross_pnl_pct": realized_gross_pct,
        "net_pnl_pct": net_pnl_pct,
        "exit_events": exit_events,
        "final_ts": final_ts,
        "minutes_in_trade": minutes_between(
            candles.iloc[0]["timestamp"],
            final_ts,
        ),
    }

def analyze_trade(
    trade,
    candles,
    post_trade_hours,
):
    side = str(trade.get("side", "")).upper()
    
    if side not in {"LONG", "SHORT"}:
        return {
            "symbol": trade.get("symbol"),
            "side": side,
            "analysis_status": "INVALID_SIDE",
        }, [], []

    entry = pd.to_numeric(
        trade.get("real_entry", trade.get("entry")),
        errors="coerce",
    )

    if pd.isna(entry):
        entry = pd.to_numeric(
            trade.get("entry"),
            errors="coerce",
        )

    tp = pd.to_numeric(trade.get("tp"), errors="coerce")
    original_sl = pd.to_numeric(trade.get("sl"), errors="coerce")

    compression_low = pd.to_numeric(
        trade.get("compression_low"),
        errors="coerce",
    )

    compression_high = pd.to_numeric(
        trade.get("compression_high"),
        errors="coerce",
    )

    entry_ts = parse_timestamp(trade.get("entry_ts"))
    exit_ts = parse_timestamp(trade.get("exit_ts"))
    
    exit_reason_group = classify_exit_reason(
        trade.get("exit_reason")
    )

    base_result = {
        "symbol": trade.get("symbol"),
        "side": side,
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "exit_reason": trade.get("exit_reason"),
        "exit_reason_group": exit_reason_group,
        "pnl": trade.get("pnl"),
        "entry": entry,
        "tp": tp,
        "sl": original_sl,
        "compression_low": compression_low,
        "compression_high": compression_high,
        "post_trade_hours": post_trade_hours,
    }

    required_price_columns = {
        "timestamp",
        "high",
        "low",
        "mark_high",
        "mark_low",
    }

    if (
        candles.empty
        or not required_price_columns.issubset(candles.columns)
        or pd.isna(entry)
        or pd.isna(tp)
        or pd.isna(entry_ts)
    ):
        base_result["analysis_status"] = "MISSING_DATA"
        base_result["missing_price_columns"] = ",".join(
            sorted(
                required_price_columns
                - set(candles.columns)
            )
        )
        return base_result, [], []

    base_result["analysis_status"] = "OK"
    
    if pd.isna(exit_ts):
        after_exit_candles = pd.DataFrame(
            columns=candles.columns
        )
    else:
        after_exit_start = exit_ts.ceil("min")

        after_exit_candles = candles[
            candles["timestamp"] >= after_exit_start
        ].copy()
    
    base_result["max_trade_mark_low_diff_pct"] = (
        (
            candles["low"] - candles["mark_low"]
        ).abs()
        / candles["mark_low"]
        * 100
    ).max()

    base_result["max_trade_mark_high_diff_pct"] = (
        (
            candles["high"] - candles["mark_high"]
        ).abs()
        / candles["mark_high"]
        * 100
    ).max()

    if side == "LONG":
        max_favorable_price = candles["high"].max()
        max_adverse_price = candles["low"].min()
    else:
        max_favorable_price = candles["low"].min()
        max_adverse_price = candles["high"].max()

    base_result["post_max_favorable_price"] = max_favorable_price
    base_result["post_max_adverse_price"] = max_adverse_price

    base_result["post_max_favorable_pct"] = favorable_move_pct(
        max_favorable_price,
        entry,
        side,
    )

    base_result["post_max_adverse_pct"] = adverse_move_pct(
        max_adverse_price,
        entry,
        side,
    )

    original_tp_pct = favorable_move_pct(tp, entry, side)

    base_result["original_tp_pct"] = original_tp_pct

    base_result["extra_move_after_tp_pct"] = max(
        0,
        base_result["post_max_favorable_pct"] - original_tp_pct,
    )
    
    if after_exit_candles.empty:
        base_result["after_exit_max_favorable_price"] = np.nan
        base_result["after_exit_max_adverse_price"] = np.nan
        base_result["after_exit_max_favorable_pct"] = np.nan
        base_result["after_exit_max_adverse_pct"] = np.nan
        base_result["extra_move_after_exit_pct"] = np.nan

    else:
        if side == "LONG":
            after_exit_max_favorable_price = after_exit_candles["high"].max()
            after_exit_max_adverse_price = after_exit_candles["low"].min()
        else:
            after_exit_max_favorable_price = after_exit_candles["low"].min()
            after_exit_max_adverse_price = after_exit_candles["high"].max()

        after_exit_max_favorable_pct = favorable_move_pct(
            after_exit_max_favorable_price,
            entry,
            side,
        )

        after_exit_max_adverse_pct = adverse_move_pct(
            after_exit_max_adverse_price,
            entry,
            side,
        )

        base_result["after_exit_max_favorable_price"] = (
            after_exit_max_favorable_price
        )

        base_result["after_exit_max_adverse_price"] = (
            after_exit_max_adverse_price
        )

        base_result["after_exit_max_favorable_pct"] = (
            after_exit_max_favorable_pct
        )

        base_result["after_exit_max_adverse_pct"] = (
            after_exit_max_adverse_pct
        )

        base_result["extra_move_after_exit_pct"] = (
            after_exit_max_favorable_pct - original_tp_pct
        )

    structural_sl = build_structural_sl(
        side=side,
        compression_low=compression_low,
        compression_high=compression_high,
        buffer_pct=0,
    )

    base_result["structural_sl_price"] = structural_sl

    if not pd.isna(structural_sl):
        base_result["structural_sl_risk_pct"] = adverse_move_pct(
            structural_sl,
            entry,
            side,
        )

        structural_replay = replay_scenario(
            candles=candles,
            tp_price=tp,
            sl_price=structural_sl,
            side=side,
        )

        base_result["structural_result"] = structural_replay["result"]
        base_result["structural_first_touch"] = structural_replay["first_touch"]
        base_result["structural_touch_ts"] = structural_replay["touch_ts"]
        base_result["minutes_to_structural_result"] = minutes_between(
            entry_ts,
            structural_replay["touch_ts"],
        )
    else:
        base_result["structural_sl_risk_pct"] = np.nan
        base_result["structural_result"] = "NO_COMPRESSION_LEVEL"
        base_result["structural_first_touch"] = None
        base_result["structural_touch_ts"] = pd.NaT
        base_result["minutes_to_structural_result"] = np.nan

    base_result["saved_by_compression_level"] = (
        exit_reason_group == "SL"
        and base_result["structural_result"] == "TP"
    )

    scenario_rows = []

    for buffer_pct in STRUCTURAL_SL_BUFFERS_PCT:
        scenario_sl = build_structural_sl(
            side=side,
            compression_low=compression_low,
            compression_high=compression_high,
            buffer_pct=buffer_pct,
        )

        if pd.isna(scenario_sl):
            continue

        for target_pct in TP_TARGETS_PCT:
            if side == "LONG":
                scenario_tp = entry * (1 + target_pct / 100)
            else:
                scenario_tp = entry * (1 - target_pct / 100)

            replay = replay_scenario(
                candles=candles,
                tp_price=scenario_tp,
                sl_price=scenario_sl,
                side=side,
            )

            risk_pct = adverse_move_pct(
                scenario_sl,
                entry,
                side,
            )

            if replay["result"] == "TP":
                simulated_pnl_pct = target_pct
            elif replay["result"] == "SL":
                simulated_pnl_pct = -risk_pct
            else:
                simulated_pnl_pct = np.nan

            scenario_rows.append({
                "symbol": trade.get("symbol"),
                "side": side,
                "entry_ts": entry_ts,
                "exit_ts": exit_ts,
                "original_exit_reason": trade.get("exit_reason"),
                "original_pnl": trade.get("pnl"),
                "entry": entry,
                "compression_low": compression_low,
                "compression_high": compression_high,
                "sl_buffer_pct": buffer_pct,
                "scenario_sl": scenario_sl,
                "structural_risk_pct": risk_pct,
                "tp_target_pct": target_pct,
                "scenario_tp": scenario_tp,
                "result": replay["result"],
                "first_touch": replay["first_touch"],
                "touch_ts": replay["touch_ts"],
                "minutes_to_result": minutes_between(
                    entry_ts,
                    replay["touch_ts"],
                ),
                "simulated_pnl_pct": simulated_pnl_pct,
            })
            
    partial_rows = []

    for strategy_name, fractions in PARTIAL_STRATEGIES.items():
        for sl_policy in PARTIAL_SL_POLICIES:
            partial_result = replay_partial_strategy(
                candles=candles,
                entry=entry,
                side=side,
                structural_sl=structural_sl,
                targets_pct=PARTIAL_TARGETS_PCT,
                fractions=fractions,
                sl_policy=sl_policy,
                cost_pct=0.10,
            )

            partial_rows.append({
                "symbol": trade.get("symbol"),
                "side": side,
                "entry_ts": entry_ts,
                "exit_ts": exit_ts,
                "original_exit_reason": trade.get("exit_reason"),
                "original_pnl": trade.get("pnl"),

                "strategy_name": strategy_name,
                "sl_policy": sl_policy,

                "structural_sl": structural_sl,
                "structural_risk_pct": (
                    adverse_move_pct(
                        structural_sl,
                        entry,
                        side,
                    )
                    if not pd.isna(structural_sl)
                    else np.nan
                ),

                "tp1_pct": PARTIAL_TARGETS_PCT[0],
                "tp2_pct": PARTIAL_TARGETS_PCT[1],
                "tp3_pct": PARTIAL_TARGETS_PCT[2],
                "tp4_pct": PARTIAL_TARGETS_PCT[3],

                "tp1_fraction": fractions[0],
                "tp2_fraction": fractions[1],
                "tp3_fraction": fractions[2],
                "tp4_fraction": fractions[3],

                **partial_result,
            })

    return base_result, scenario_rows, partial_rows

def fetch_candles_by_symbol(
    trades,
    post_trade_hours,
):
    candles_by_symbol = {}

    for symbol, symbol_trades in trades.groupby("symbol"):
        entry_times = symbol_trades["entry_ts"].apply(
            parse_timestamp
        )

        if "exit_ts" in symbol_trades.columns:
            exit_times = symbol_trades["exit_ts"].apply(
                parse_timestamp
            )
        else:
            exit_times = pd.Series(
                pd.NaT,
                index=symbol_trades.index,
            )

        valid_entries = entry_times.dropna()

        if valid_entries.empty:
            continue

        start = valid_entries.min().floor("min")

        reference_times = exit_times.copy()
        reference_times = reference_times.where(
            reference_times.notna(),
            entry_times,
        )

        valid_references = reference_times.dropna()

        end = (
            valid_references.max()
            + pd.Timedelta(hours=post_trade_hours)
        ).ceil("min")

        print(
            f"[FETCH] {symbol} | "
            f"{start} -> {end} | "
            f"timeframe=1m | trade+mark"
        )

        try:
            candles_by_symbol[symbol] = fetch_replay_history(
                symbol=symbol,
                since_ts=start,
                until_ts=end,
            )

        except Exception as exc:
            print(f"[ERROR] {symbol}: {exc}")
            candles_by_symbol[symbol] = pd.DataFrame()

        time.sleep(exchange.rateLimit / 1000)

    return candles_by_symbol

def build_reports(
    trades_file,
    report_file,
    scenarios_file,
    partials_file,
    post_trade_hours,
):
    trades = pd.read_csv(trades_file)

    required = [
        "symbol",
        "side",
        "entry_ts",
        "exit_ts",
        "tp",
        "sl",
        "compression_low",
        "compression_high",
        "exit_reason",
    ]

    missing = [
        column
        for column in required
        if column not in trades.columns
    ]

    if missing:
        raise ValueError(
            f"Missing trades columns: {missing}"
        )

    trades = trades.copy()
    
    trades = trades[
        trades["exit_ts"].notna()
        & trades["exit_reason"].notna()
    ].copy()

    trades["entry_ts_parsed"] = trades["entry_ts"].apply(
        parse_timestamp
    )

    trades = trades.dropna(
        subset=["symbol", "entry_ts_parsed"],
    )

    candles_by_symbol = fetch_candles_by_symbol(
        trades=trades,
        post_trade_hours=post_trade_hours,
    )

    replay_rows = []
    scenario_rows = []
    partial_rows = []

    for _, trade in trades.iterrows():
        symbol = trade["symbol"]
        entry_ts = parse_timestamp(trade["entry_ts"])

        exit_ts = parse_timestamp(trade.get("exit_ts"))

        window_reference = (
            exit_ts
            if not pd.isna(exit_ts)
            else entry_ts
        )

        window_end = (
            window_reference
            + pd.Timedelta(hours=post_trade_hours)
        )
        symbol_candles = candles_by_symbol.get(
            symbol,
            pd.DataFrame(),
        )

        if symbol_candles.empty:
            trade_candles = pd.DataFrame()
        else:
            replay_start = entry_ts.ceil("min")

            trade_candles = symbol_candles[
                (symbol_candles["timestamp"] >= replay_start)
                & (symbol_candles["timestamp"] <= window_end)
            ].copy()

        (
            replay_row,
            trade_scenarios,
            trade_partials,
        ) = analyze_trade(
            trade=trade,
            candles=trade_candles,
            post_trade_hours=post_trade_hours,
        )

        replay_rows.append(replay_row)
        scenario_rows.extend(trade_scenarios)
        partial_rows.extend(trade_partials)

    replay_df = pd.DataFrame(replay_rows)
    scenarios_df = pd.DataFrame(scenario_rows)
    partials_df = pd.DataFrame(partial_rows)

    report_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    replay_df.to_csv(
        report_file,
        index=False,
    )

    scenarios_df.to_csv(
        scenarios_file,
        index=False,
    )
    
    partials_df.to_csv(
        partials_file,
        index=False,
    )

    print()
    print(f"[OK] Replay report: {report_file}")
    print(f"[OK] Scenario report: {scenarios_file}")
    print(f"[OK] Trades analyzed: {len(replay_df)}")
    print(f"[OK] Scenarios generated: {len(scenarios_df)}")
    print(f"[OK] Partial report: {partials_file}")
    print(f"[OK] Partial scenarios: {len(partials_df)}")

    if "saved_by_compression_level" in replay_df.columns:
        saved = replay_df[
            "saved_by_compression_level"
        ].fillna(False).sum()

        print(f"[RESULT] SL trades saved: {int(saved)}")
        
def main():
    parser = argparse.ArgumentParser(
        description=(
            "Replay trades using 1m candles and compare "
            "original TP/SL against compression structural levels."
        )
    )
    
    parser.add_argument(
        "--partials-output",
        default=str(DEFAULT_PARTIALS_FILE),
    )

    parser.add_argument(
        "--trades",
        default=str(DEFAULT_TRADES_FILE),
    )

    parser.add_argument(
        "--output",
        default=str(DEFAULT_REPORT_FILE),
    )

    parser.add_argument(
        "--scenarios-output",
        default=str(DEFAULT_SCENARIOS_FILE),
    )

    parser.add_argument(
        "--hours",
        type=int,
        default=24,
    )

    args = parser.parse_args()

    build_reports(
        trades_file=Path(args.trades),
        report_file=Path(args.output),
        scenarios_file=Path(args.scenarios_output),
        partials_file=Path(args.partials_output),
        post_trade_hours=args.hours,
    )


if __name__ == "__main__":
    main()