from dotenv import load_dotenv
import time
import json
from pathlib import Path
import pandas as pd

from engine.live.data.data_buffer import DataBuffer
from signals.signals_engine import SignalEngine
from engine.live.strategy.entry_engine import EntryEngine
from data.market_data import fetch_history
from ui.banners import print_live_banner


# =========================
# LOAD ENV
# =========================
load_dotenv()


# =========================
# CONFIG
# =========================
SYMBOL = "BTCUSDT"
TRIGGER_TF = "15m"
DAYS = 30
SLEEP = 0
ARG_TZ = "America/Argentina/Cordoba"

BASE_DIR = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = BASE_DIR / "chart_data"
OUTPUT_DIR.mkdir(exist_ok=True)

SIGNALS_CSV_PATH = OUTPUT_DIR / f"replay_signals_{SYMBOL}.csv"
PLANS_CSV_PATH = OUTPUT_DIR / f"replay_plans_{SYMBOL}.csv"
CHART_EVENTS_PATH = OUTPUT_DIR / f"chart_events_{SYMBOL}.jsonl"


# =========================
# INIT
# =========================
buffer = DataBuffer()
signals = SignalEngine(buffer)
entry_engine = EntryEngine(buffer)

print_live_banner()
print("\033[95m[REPLAY]\033[0m 🎬 Replay mode (ENTRY EVENTS)\n")


# =========================
# HELPERS
# =========================
def normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    if not isinstance(df.iloc[0]["timestamp"], (int, float)):
        df["timestamp"] = df["timestamp"].apply(lambda x: int(x.timestamp() * 1000))

    return df


def row_to_candle(row) -> dict:
    return {
        "timestamp": int(row["timestamp"]),
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
    }


def build_merged_candle_stream(df_5m, df_15m, df_1h):
    all_candles = []

    for _, row in df_5m.iterrows():
        all_candles.append((int(row["timestamp"]), "5m", row_to_candle(row)))

    for _, row in df_15m.iterrows():
        all_candles.append((int(row["timestamp"]), "15m", row_to_candle(row)))

    for _, row in df_1h.iterrows():
        all_candles.append((int(row["timestamp"]), "1h", row_to_candle(row)))

    all_candles.sort(key=lambda x: x[0])
    return all_candles


def to_local_dt_str(ts_ms: int, tz_name: str) -> str:
    return (
        pd.to_datetime(ts_ms, unit="ms", utc=True)
        .tz_convert(tz_name)
        .strftime("%Y-%m-%d %H:%M:%S")
    )


def build_signal_event(signal: dict, trigger_tf: str, fallback_price: float, signal_ts: int) -> dict:
    side = signal.get("side")
    signal_price = signal.get("signal_price", signal.get("price", fallback_price))
    trend = signal.get("trend")
    direction = signal.get("direction", signal.get("dir"))
    momentum = signal.get("momentum")

    return {
        "event_type": "signal_debug",
        "symbol": SYMBOL,
        "timestamp": int(signal_ts),
        "timestamp_local": to_local_dt_str(int(signal_ts), ARG_TZ),
        "tf": trigger_tf,
        "side": side,
        "signal_price": float(signal_price),
        "trend": trend,
        "direction": direction,
        "momentum": momentum,
        "marker_text": f"{side} | {trend} / {direction} / {momentum}",
    }


def build_entry_event(plan) -> dict:
    # 🔥 CLAVE: usar signal_ts como timestamp del evento
    # porque plan.timestamp te estaba quedando mal y se iba a 1969
    entry_ts_ms = int(plan.signal_ts)
    signal_ts_ms = int(plan.signal_ts)

    trend = plan.signal_context.get("trend") if plan.signal_context else None
    direction = plan.signal_context.get("direction") if plan.signal_context else None
    momentum = plan.signal_context.get("momentum") if plan.signal_context else None

    return {
        "event_type": "entry",
        "symbol": plan.symbol,
        "timestamp": entry_ts_ms,
        "timestamp_local": to_local_dt_str(entry_ts_ms, ARG_TZ),
        "signal_timestamp": signal_ts_ms,
        "signal_timestamp_local": to_local_dt_str(signal_ts_ms, ARG_TZ),
        "tf": TRIGGER_TF,
        "side": plan.side,
        "signal_price": float(plan.signal_price),
        "entry_price": float(plan.entry),
        "tp": float(plan.tp),
        "sl": float(plan.sl),
        "atr": float(plan.atr),
        "trend": trend,
        "direction": direction,
        "momentum": momentum,
        "marker_text": (
            f"{plan.side} | entry={plan.entry} | tp={plan.tp} | "
            f"sl={plan.sl} | atr={plan.atr} | {trend}/{direction}/{momentum}"
        ),
    }


def is_valid_signal_context(signal: dict) -> bool:
    if not signal:
        return False

    side = signal.get("side")
    trend = signal.get("trend")
    direction = signal.get("direction", signal.get("dir"))
    momentum = signal.get("momentum")

    if side not in ("LONG", "SHORT"):
        return False

    # 🚨 filtro fuerte para sacar ruido
    if trend == "neutral":
        return False

    if direction in (None, "", "range", "none"):
        return False

    if momentum in ("inside_bar", "indecision"):
        return False

    return True


def is_new_plan(plan, last_plan) -> bool:
    if last_plan is None:
        return True

    if plan.side != last_plan.side:
        return True

    # cambio suficientemente significativo
    if abs(plan.entry - last_plan.entry) > (plan.atr * 0.20):
        return True

    if abs(plan.tp - last_plan.tp) > (plan.atr * 0.20):
        return True

    if abs(plan.sl - last_plan.sl) > (plan.atr * 0.20):
        return True

    return False


def process_closed_candle(buffer, signals, entry_engine, tf: str, candle: dict, trigger_tf: str = "15m"):
    buffer.on_replay_candle(candle, tf)

    if tf != trigger_tf:
        return None

    if not buffer.consume_closed_tf(trigger_tf):
        return None

    signal_ts = int(buffer.last_close_time[trigger_tf])
    price = candle["close"]

    print(f"\033[95m[SYNC]\033[0m 🕒 {trigger_tf} CLOSED @ {signal_ts}")

    signal = signals.generate_signal()

    if not signal:
        print("\033[93m[SIGNAL]\033[0m ❌ No signal\n")
        return {
            "signal": None,
            "signal_event": None,
            "plan": None,
            "entry_event": None,
        }

    signal.setdefault("signal_ts", signal_ts)
    signal.setdefault("signal_price", signal.get("price", price))

    print(
        f"\033[92m[SIGNAL]\033[0m ✅ {signal.get('side')} | "
        f"{signal.get('trend')} / {signal.get('direction', signal.get('dir'))} / {signal.get('momentum')}"
    )

    signal_event = build_signal_event(
        signal=signal,
        trigger_tf=trigger_tf,
        fallback_price=price,
        signal_ts=signal_ts,
    )

    # 🔥 filtro de contexto ANTES del entry engine
    if not is_valid_signal_context(signal):
        print("\033[91m[FILTER]\033[0m ❌ Signal context rejected\n")
        return {
            "signal": signal,
            "signal_event": signal_event,
            "plan": None,
            "entry_event": None,
        }

    plan = entry_engine.generate_entry(signal)

    if not plan:
        print("\033[91m[ENTRY]\033[0m ❌ No entry plan\n")
        return {
            "signal": signal,
            "signal_event": signal_event,
            "plan": None,
            "entry_event": None,
        }

    entry_event = build_entry_event(plan)

    print("\033[96m[ENTRY]\033[0m ✅ Plan generated\n")

    return {
        "signal": signal,
        "signal_event": signal_event,
        "plan": plan,
        "entry_event": entry_event,
    }


def save_signals_csv(rows: list[dict], csv_path: Path):
    if not rows:
        print("\033[95m[REPLAY]\033[0m ℹ️ No signals to save")
        return

    df = pd.DataFrame(rows)
    df["timestamp"] = (
        pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        .dt.tz_convert(ARG_TZ)
        .dt.tz_localize(None)
    )
    df.sort_values("timestamp", inplace=True)
    df.to_csv(csv_path, index=False)
    print(f"\033[95m[REPLAY]\033[0m 💾 Saved signals CSV to {csv_path}")


def save_plans_csv(rows: list[dict], csv_path: Path):
    if not rows:
        print("\033[95m[REPLAY]\033[0m ℹ️ No plans to save")
        return

    df = pd.DataFrame(rows)

    for col in ["timestamp", "signal_timestamp"]:
        if col in df.columns:
            df[col] = (
                pd.to_datetime(df[col], unit="ms", utc=True)
                .dt.tz_convert(ARG_TZ)
                .dt.tz_localize(None)
            )

    df.sort_values("timestamp", inplace=True)
    df.to_csv(csv_path, index=False)
    print(f"\033[95m[REPLAY]\033[0m 💾 Saved plans CSV to {csv_path}")


def save_chart_events_jsonl(events: list[dict], jsonl_path: Path):
    if not events:
        print("\033[95m[REPLAY]\033[0m ℹ️ No chart events to save")
        return

    with open(jsonl_path, "w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    print(f"\033[95m[REPLAY]\033[0m 💾 Saved chart events JSONL to {jsonl_path}")


# =========================
# LOAD DATA
# =========================
df_5m = normalize_df(fetch_history(SYMBOL, "5m", DAYS))
df_15m = normalize_df(fetch_history(SYMBOL, "15m", DAYS))
df_1h = normalize_df(fetch_history(SYMBOL, "1h", DAYS))

all_candles = build_merged_candle_stream(df_5m, df_15m, df_1h)


# =========================
# STORAGE
# =========================
all_signals = []
all_plans = []
chart_events = []

last_saved_plan = None


# =========================
# REPLAY LOOP
# =========================
try:
    for ts, tf, candle in all_candles:
        result = process_closed_candle(
            buffer=buffer,
            signals=signals,
            entry_engine=entry_engine,
            tf=tf,
            candle=candle,
            trigger_tf=TRIGGER_TF,
        )

        if result and result.get("signal_event"):
            signal_event = result["signal_event"]
            all_signals.append({
                "timestamp": signal_event["timestamp"],
                "tf": signal_event["tf"],
                "side": signal_event["side"],
                "signal_price": signal_event["signal_price"],
                "direction": signal_event["direction"],
                "trend": signal_event["trend"],
                "momentum": signal_event["momentum"],
            })

        if result and result.get("plan"):
            plan = result["plan"]

            if is_new_plan(plan, last_saved_plan):
                entry_event = result["entry_event"]
                chart_events.append(entry_event)

                all_plans.append({
                    "timestamp": entry_event["timestamp"],
                    "signal_timestamp": entry_event["signal_timestamp"],
                    "tf": entry_event["tf"],
                    "side": entry_event["side"],
                    "signal_price": entry_event["signal_price"],
                    "entry_price": entry_event["entry_price"],
                    "tp": entry_event["tp"],
                    "sl": entry_event["sl"],
                    "atr": entry_event["atr"],
                    "direction": entry_event["direction"],
                    "trend": entry_event["trend"],
                    "momentum": entry_event["momentum"],
                })

                last_saved_plan = plan

        if SLEEP > 0:
            time.sleep(SLEEP)

    print("\033[95m[REPLAY]\033[0m ✅ Replay finished")

except KeyboardInterrupt:
    print("\033[95m[REPLAY]\033[0m 🛑 Replay stopped")


# =========================
# DEBUG SUMMARY
# =========================
print(f"\033[94m[DEBUG]\033[0m signals saved: {len(all_signals)}")
print(f"\033[94m[DEBUG]\033[0m plans saved: {len(all_plans)}")
print(f"\033[94m[DEBUG]\033[0m chart events saved: {len(chart_events)}")


# =========================
# SAVE OUTPUTS
# =========================
save_signals_csv(all_signals, SIGNALS_CSV_PATH)
save_plans_csv(all_plans, PLANS_CSV_PATH)
save_chart_events_jsonl(chart_events, CHART_EVENTS_PATH)