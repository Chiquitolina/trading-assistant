import ccxt
import pandas as pd

from ta.volatility import AverageTrueRange

from indicators.trend import trend_bias
from indicators.direction import trade_direction
from indicators.momentum import momentum_5m
from config.timeframes import TIMEFRAME_CONFIGS

from colorama import Fore, Style, init
init(autoreset=True)

# --------------------
# EXCHANGE
# --------------------
exchange = ccxt.binanceusdm({
    "enableRateLimit": True
})

# --------------------
# DATA
# --------------------
def fetch_ohlcv(symbol, timeframe, candles):
    ohlcv = exchange.fetch_ohlcv(
        symbol,
        timeframe=timeframe,
        limit=candles
    )

    return pd.DataFrame(
        ohlcv,
        columns=["timestamp", "open", "high", "low", "close", "volume"]
    )


# --------------------
# FILTERS
# --------------------
def atr_is_expanding(df, period, expansion_factor):
    atr = AverageTrueRange(
        high=df["high"],
        low=df["low"],
        close=df["close"],
        window=period
    ).average_true_range()

    current_atr = atr.iloc[-1]
    mean_atr = atr.iloc[-period:].mean()

    return current_atr > mean_atr * expansion_factor


def volume_above_average(df, lookback):
    current_volume = df["volume"].iloc[-1]
    avg_volume = df["volume"].iloc[-lookback:].mean()

    return current_volume > avg_volume


# --------------------
# FORMATTERS
# --------------------
def color_trend(trend: str) -> str:
    if trend == "bullish":
        return f"{Fore.GREEN}BULLISH{Style.RESET_ALL}"
    if trend == "bearish":
        return f"{Fore.RED}BEARISH{Style.RESET_ALL}"
    return "neutral"


def color_direction(direction: str) -> str:
    if direction == "up":
        return f"{Fore.GREEN}UP ⬆{Style.RESET_ALL}"
    if direction == "down":
        return f"{Fore.RED}DOWN ⬇{Style.RESET_ALL}"
    return direction.upper()


def color_momentum(m: str) -> str:
    if m == "breakout_up":
        return f"{Fore.GREEN}BO ↑{Style.RESET_ALL}"
    if m == "breakout_down":
        return f"{Fore.RED}BO ↓{Style.RESET_ALL}"
    return "—"


# --------------------
# SCANNER
# --------------------
def scan_market():
    cfg_trend = TIMEFRAME_CONFIGS["1h"]
    cfg_trade = TIMEFRAME_CONFIGS["15m"]
    cfg_momentum = TIMEFRAME_CONFIGS["5m"]

    print(
        f"\n🔎 Scan iniciado | "
        f"1H → Trend | 15m → Setup | 5m → Momentum\n"
    )

    markets = exchange.load_markets()

    symbols = [
        s for s, m in markets.items()
        if m.get("active")
        and m.get("quote") == "USDT"
        and m.get("contract") is True
    ]

    tradeable = []

    for symbol in symbols:
        try:
            ticker = exchange.fetch_ticker(symbol)

            if ticker["quoteVolume"] < cfg_trade["min_quote_volume"]:
                continue

            # -------- DATA --------
            df_trend = fetch_ohlcv(
                symbol,
                cfg_trend["timeframe"],
                cfg_trend["candles"]
            )

            df_trade = fetch_ohlcv(
                symbol,
                cfg_trade["timeframe"],
                cfg_trade["candles"]
            )

            df_momentum = fetch_ohlcv(
                symbol,
                cfg_momentum["timeframe"],
                cfg_momentum["candles"]
            )

            # -------- INDICATORS --------
            trend = trend_bias(df_trend)
            direction = trade_direction(df_trade)
            momentum = momentum_5m(df_momentum)

            atr_ok = atr_is_expanding(
                df_trade,
                cfg_trade["atr_period"],
                cfg_trade["atr_expansion"]
            )

            vol_ok = volume_above_average(
                df_trade,
                cfg_trade["volume_lookback"]
            )

            # -------- LOGIC --------
            long_ok = (
                trend == "bullish"
                and direction == "up"
                and momentum == "breakout_up"
                and atr_ok
                and vol_ok
            )

            # -------- OUTPUT --------
            print(
                f"{symbol} | "
                f"Trend: {color_trend(trend)} | "
                f"Dir: {color_direction(direction)} | "
                f"Mom: {color_momentum(momentum)} | "
                f"ATR: {atr_ok} | VOL: {vol_ok}"
            )

            if long_ok:
                tradeable.append({
                    "symbol": symbol,
                    "trend": trend,
                    "direction": direction,
                    "momentum": momentum,
                    "quote_volume": ticker["quoteVolume"]
                })

        except Exception as e:
            print(f"⚠ Error en {symbol}: {e}")

    print(f"\n✅ Scan finalizado | Encontrados: {len(tradeable)} pares\n")
    return tradeable
