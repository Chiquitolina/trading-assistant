import pandas as pd
from signals.indicators.trend import trend_bias
from signals.indicators.momentum import momentum_5m
from signals.indicators.direction import trade_direction
from signals.strategy.entries import long_setup, short_setup
from enums.trend import Trend
from enums.direction import Direction
from enums.momentum import Momentum
from models.signals import Signal
from ta.trend import EMAIndicator

from signals.indicators.atr import add_atr
from signals.indicators.micro_momentum import micro_momentum_1m

from config.timeframes import MODE_CONFIG

MODE = MODE_CONFIG["aggressive"]

SWING_LOOKBACK = 3
MIN_SWING_MOVE_PCT = 0.00025

def ema_distance_pct(price: float, ema: float | None) -> float | None:
    if ema is None or pd.isna(ema) or ema <= 0:
        return None

    return round(((price - ema) / ema) * 100, 4)

def detect_swing_high(df, idx, lookback=2):
    if idx < lookback:
        return False

    current_high = df.iloc[idx]["high"]
    left_max = df.iloc[idx - lookback:idx]["high"].max()

    move_pct = (current_high - left_max) / left_max

    return current_high > left_max and move_pct >= MIN_SWING_MOVE_PCT


def detect_swing_low(df, idx, lookback=2):
    if idx < lookback:
        return False

    current_low = df.iloc[idx]["low"]
    left_min = df.iloc[idx - lookback:idx]["low"].min()

    move_pct = (left_min - current_low) / left_min

    return current_low < left_min and move_pct >= MIN_SWING_MOVE_PCT


def find_last_swing_low(df, idx):
    for j in range(idx - 1, SWING_LOOKBACK, -1):
        if detect_swing_low(df, j, SWING_LOOKBACK):
            return df.iloc[j]["low"]

    return None


def find_last_swing_high(df, idx):
    for j in range(idx - 1, SWING_LOOKBACK, -1):
        if detect_swing_high(df, j, SWING_LOOKBACK):
            return df.iloc[j]["high"]

    return None

class SignalEngine:
    def __init__(self, buffer, debug=True, mode="default"):
        self.buffer = buffer
        self.debug = debug
        self.mode = mode
        self.last_signal_ts = {}
        self.momentum_history = {}

    def get_trend(self, symbol):
        df = pd.DataFrame(self.buffer.get_candles(symbol, "1h"))
        if len(df) < 20:
            return Trend.NEUTRAL

        df = trend_bias(df)
        return df.iloc[-1]["trend"]


    def get_direction(self, symbol):
        df = pd.DataFrame(self.buffer.get_candles(symbol, "15m"))
        if len(df) < 10:
            return Direction.UNKNOWN

        result = trade_direction(df)

        if isinstance(result, pd.DataFrame):
            return result.iloc[-1]["direction"]

        return result


    def get_momentum(self, symbol):
        df = pd.DataFrame(self.buffer.get_candles(symbol, "5m"))
        if len(df) < 2:
            return Momentum.NO_DATA

        result = momentum_5m(df)

        if isinstance(result, pd.DataFrame):
            return result.iloc[-1]["momentum"]

        return result

    def generate_signal(self, symbol: str):
        if self.mode == "aggressive":
            return self.generate_aggressive_signal(symbol)

        return self.generate_default_signal(symbol)

    def generate_default_signal(self, symbol: str):

        candles_15m = self.buffer.get_candles(symbol, "15m")

        if not candles_15m:
            return None

        last_candle = candles_15m[-1]

        signal_price = last_candle["close"]

        tf_ms = 15 * 60 * 1000
        signal_ts = last_candle["timestamp"] + tf_ms - 1

        candles_5m = self.buffer.get_candles(symbol, "5m")

        last_5m_ts = candles_5m[-1]["timestamp"] if candles_5m else None

        print("\033[95m[DEBUG]\033[0m ⏱ TIMESTAMPS")
        print(f"symbol        : {symbol}")
        print(f"15m signal_ts : {signal_ts}")
        print(f"5m last_ts    : {last_5m_ts}\n")

        last_ts = self.last_signal_ts.get(symbol)

        print(
            f"\033[95m[SIGNAL DEBUG]\033[0m "
            f"symbol={symbol} "
            f"signal_ts={signal_ts} "
            f"last_signal_ts={last_ts}"
        )

        if signal_ts == last_ts:
            return None

        self.last_signal_ts[symbol] = signal_ts

        df_15m = pd.DataFrame(candles_15m)
        df_5m = pd.DataFrame(candles_5m) if candles_5m else pd.DataFrame()

        trend = self.get_trend(symbol)
        direction = self.get_direction(symbol)
        momentum = self.get_momentum(symbol)

        history = self.momentum_history.setdefault(symbol, [])

        history.append(momentum)

        if len(history) > 10:
            history.pop(0)

        prev1 = history[-2] if len(history) >= 2 else None
        prev2 = history[-3] if len(history) >= 3 else None

        long_ok = long_setup(trend, direction, momentum)
        short_ok = short_setup(trend, direction, momentum)

        if self.debug:
            print("\033[94m[SIGNALS LAYER]\033[0m 📷 Snapshot")
            print(f"symbol        : {symbol}")
            print(f"1h trend      : {trend.value}")
            print(f"15m direction : {direction.value}")
            print(f"5m momentum   : {momentum.value}")
            print(f"long_ok       : {long_ok}")
            print(f"short_ok      : {short_ok}\n")

        return Signal(
            symbol=symbol,

            signal_price=round(signal_price, 2),
            signal_ts=signal_ts,

            trend=trend,
            direction=direction,
            momentum=momentum,

            momentum_prev1=prev1,
            momentum_prev2=prev2,

            momentum_sequence=[
                prev2,
                prev1,
                momentum
            ]
        )
        
    def generate_aggressive_signal(self, symbol: str):

        candles_1m = self.buffer.get_candles(symbol, "1m")
        candles_5m = self.buffer.get_candles(symbol, "5m")
        candles_15m = self.buffer.get_candles(symbol, "15m")
        candles_1h = self.buffer.get_candles(symbol, "1h")
        candles_4h = self.buffer.get_candles(symbol, "4h")

        if not candles_1m or not candles_5m:
            return None

        if len(candles_1m) < 60 or len(candles_5m) < 100:
            return None

        last_candle = candles_1m[-1]

        signal_price = last_candle["close"]

        tf_ms = 1 * 60 * 1000
        signal_ts = last_candle["timestamp"] + tf_ms - 1

        last_ts = self.last_signal_ts.get(symbol)

        if signal_ts == last_ts:
            return None

        self.last_signal_ts[symbol] = signal_ts

        df_1m = pd.DataFrame(candles_1m)
        df_5m = pd.DataFrame(candles_5m)
        
        df_15m = pd.DataFrame(candles_15m) if candles_15m else pd.DataFrame()
        df_1h = pd.DataFrame(candles_1h) if candles_1h else pd.DataFrame()
        df_4h = pd.DataFrame(candles_4h) if candles_4h else pd.DataFrame()

        df_1m = add_atr(df_1m, period=14)
        df_5m = add_atr(df_5m, period=14)

        df_5m["ema100"] = EMAIndicator(
            df_5m["close"],
            window=100
        ).ema_indicator()

        df_5m["ema20"] = EMAIndicator(
            df_5m["close"],
            window=20
        ).ema_indicator()

        for n in [20, 34, 50]:
            df_1m[f"ema_{n}"] = EMAIndicator(
                df_1m["close"],
                window=n
            ).ema_indicator()
            
        def add_htf_emas(df):
            if df.empty or len(df) < 100:
                return None, None

            df = df.copy()

            df["ema50"] = EMAIndicator(
                df["close"],
                window=50
            ).ema_indicator()

            df["ema99"] = EMAIndicator(
                df["close"],
                window=99
            ).ema_indicator()

            ema50 = df.iloc[-1]["ema50"]
            ema99 = df.iloc[-1]["ema99"]

            if pd.isna(ema50) or pd.isna(ema99):
                return None, None

            return ema50, ema99

        row = df_1m.iloc[-1]
        idx = len(df_1m) - 1

        ema20_now = df_5m.iloc[-1]["ema20"]
        ema20_prev = df_5m.iloc[-2]["ema20"]

        ema100 = df_5m.iloc[-1]["ema100"]
        price_5m = df_5m.iloc[-1]["close"]

        ema20_1m = row["ema_20"]
        ema34_1m = row["ema_34"]
        ema50_1m = row["ema_50"]

        atr_5m = df_5m.iloc[-1]["atr"]

        if (
            pd.isna(atr_5m)
            or pd.isna(ema20_now)
            or pd.isna(ema20_prev)
            or pd.isna(ema100)
            or pd.isna(ema20_1m)
            or pd.isna(ema34_1m)
            or pd.isna(ema50_1m)
        ):
            return None

        atr_5m_pct = (atr_5m / signal_price) * 100
        
        ema50_15m, ema99_15m = add_htf_emas(df_15m)
        ema50_1h, ema99_1h = add_htf_emas(df_1h)
        ema50_4h, ema99_4h = add_htf_emas(df_4h)

        dist_ema50_15m_pct = ema_distance_pct(signal_price, ema50_15m)
        dist_ema99_15m_pct = ema_distance_pct(signal_price, ema99_15m)

        dist_ema50_1h_pct = ema_distance_pct(signal_price, ema50_1h)
        dist_ema99_1h_pct = ema_distance_pct(signal_price, ema99_1h)

        dist_ema50_4h_pct = ema_distance_pct(signal_price, ema50_4h)
        dist_ema99_4h_pct = ema_distance_pct(signal_price, ema99_4h)
        
        if self.debug:
            print(
                f"\033[95m[HTF EXT]\033[0m {symbol} | "
                f"15m EMA50={dist_ema50_15m_pct}% EMA99={dist_ema99_15m_pct}% | "
                f"1h EMA50={dist_ema50_1h_pct}% EMA99={dist_ema99_1h_pct}% | "
                f"4h EMA50={dist_ema50_4h_pct}% EMA99={dist_ema99_4h_pct}%"
            )

        ema20_slope = ema20_now - ema20_prev

        bullish_condition = (
            ema20_now > ema100
            and price_5m > ema100
            and ema20_slope > 0
        )

        bearish_condition = (
            ema20_now < ema100
            and price_5m < ema100
            and ema20_slope < 0
        )

        if bullish_condition:
            htf_bullish = True
            htf_bearish = False
            trend = Trend.BULLISH

        elif bearish_condition:
            htf_bullish = False
            htf_bearish = True
            trend = Trend.BEARISH

        else:
            htf_bullish = False
            htf_bearish = False
            trend = Trend.NEUTRAL

        direction_5m = trade_direction(df_5m)

        momentum_5m_value = momentum_5m(df_5m)

        micro = micro_momentum_1m(
            df_1m,
            atr=atr_5m
        )
        
        print(
            f"\033[96m[AGGRESSIVE DEBUG]\033[0m "
            f"{symbol} | "
            f"5m={momentum_5m_value.value} | "
            f"1m={micro.value}"
        )

        history = self.momentum_history.setdefault(symbol, [])

        history.append(momentum_5m_value)

        if len(history) > 10:
            history.pop(0)

        prev1 = history[-2] if len(history) >= 2 else None
        prev2 = history[-3] if len(history) >= 3 else None

        ema_alignment_bullish = (
            ema20_1m > ema34_1m > ema50_1m
        )

        near_ema20_long = (
            signal_price > ema20_1m
            and abs(signal_price - ema20_1m) <= (atr_5m * 0.35)
        )

        near_ema20_short = (
            signal_price < ema20_1m
            and abs(signal_price - ema20_1m) <= (atr_5m * 0.35)
        )

        near_ema50_long = (
            abs(signal_price - ema50_1m) <= (atr_5m * 0.35)
        )

        swing_low = find_last_swing_low(df_1m, idx)
        swing_high = find_last_swing_high(df_1m, idx)

        near_swing_low = (
            swing_low is not None
            and abs(signal_price - swing_low) <= (atr_5m * 1.2)
        )

        near_swing_high = (
            swing_high is not None
            and abs(signal_price - swing_high) <= (atr_5m * 1.2)
        )

        return Signal(
            symbol=symbol,

            signal_price=round(signal_price, 2),
            signal_ts=signal_ts,

            trend=trend,
            direction=direction_5m,
            momentum=momentum_5m_value,
            micro=micro,

            momentum_prev1=prev1,
            momentum_prev2=prev2,

            momentum_sequence=[
                prev2,
                prev1,
                momentum_5m_value,
            ],

            atr_5m=atr_5m,
            atr_5m_pct=atr_5m_pct,

            ema20_1m=ema20_1m,
            ema34_1m=ema34_1m,
            ema50_1m=ema50_1m,

            ema20_5m=ema20_now,
            ema100_5m=ema100,
            
                        
            ema50_15m=ema50_15m,
            ema99_15m=ema99_15m,
            ema50_1h=ema50_1h,
            ema99_1h=ema99_1h,
            ema50_4h=ema50_4h,
            ema99_4h=ema99_4h,

            dist_ema50_15m_pct=dist_ema50_15m_pct,
            dist_ema99_15m_pct=dist_ema99_15m_pct,
            dist_ema50_1h_pct=dist_ema50_1h_pct,
            dist_ema99_1h_pct=dist_ema99_1h_pct,
            dist_ema50_4h_pct=dist_ema50_4h_pct,
            dist_ema99_4h_pct=dist_ema99_4h_pct,

            htf_bullish=htf_bullish,
            htf_bearish=htf_bearish,

            ema_alignment_bullish=ema_alignment_bullish,

            near_ema20_long=near_ema20_long,
            near_ema20_short=near_ema20_short,
            near_ema50_long=near_ema50_long,

            swing_low=swing_low,
            swing_high=swing_high,

            near_swing_low=near_swing_low,
            near_swing_high=near_swing_high,
        )