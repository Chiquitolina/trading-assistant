import ccxt
import pandas as pd

from ta.volatility import AverageTrueRange

from common.indicators.trend import trend_bias
from indicators.direction import trade_direction
from common.indicators.momentum import momentum_5m
from common.indicators.volume.volume_metrics import VolumeMetrics
from config.timeframes import TIMEFRAME_CONFIGS

from config.timeframes import TIMEFRAMES
from config.volume_config import VOLUME_CONFIG
from config.volatily_config import ATR_CONFIG

from ui.formatters import color_direction, color_momentum, color_trend

from data.ohlcv import fetch_ohlcv
from scanner.filters.volatily import atr_is_expanding

from infra.exchange import get_exchange

exchange = get_exchange()

# --------------------
# SCANNER
# --------------------
def scan_market():
    
    
    TF_TREND = "1h"
    TF_SETUP = "15m"
    TF_MOMENTUM = "5m"
    TF_VOLUME = "1h"   # 👈 clave
    
    trend_tf = TIMEFRAMES[TF_TREND]
    setup_tf = TIMEFRAMES[TF_SETUP]
    momentum_tf = TIMEFRAMES[TF_MOMENTUM]
    
    cfg_trade = TIMEFRAME_CONFIGS["15m"]

    vol_cfg = VOLUME_CONFIG[TF_VOLUME]
    atr_cfg = ATR_CONFIG[TF_SETUP]

    print(
        f"\n------------------------------------------------------------------------------\n"
        f"1H → Trend | 15m → Direction (Setup) | 5m → Momentum\n"
        f"\n------------------------------------------------------------------------------\n"
        f"\n"
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
                trend_tf["tf"],
                trend_tf["candles"]
            )

            df_trade = fetch_ohlcv(
                symbol,
                setup_tf["tf"],
                setup_tf["candles"]
            )

            df_momentum = fetch_ohlcv(
                symbol,
                momentum_tf["tf"],
                momentum_tf["candles"]
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

            volume = VolumeMetrics().calc(
                df_trade,
                period=cfg_trade["volume_lookback"]
            )
            
            vol_ok = volume and volume["state"] in ["growing", "spike"]

            # -------- LOGIC --------
            long_ok = (
                trend == "bullish"
                and direction == "up"
                and momentum == "breakout_up"
                and atr_ok
                and vol_ok
            )
            
            vol_str = (
                f"{volume['state']} ({volume['rvol']})"
                if volume else "n/a"
            )

            # -------- OUTPUT --------
            print(
                f"{symbol} \n"
                f"    | Trend (1H): {color_trend(trend)} \n"
                f"    | Direction/Setup (15m): {color_direction(direction)} \n"
                f"    | Momentum (5m): {color_momentum(momentum)} \n"
                f"    | VOL: {vol_str} \n"
                f"    | ATR: {atr_ok} \n"

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
