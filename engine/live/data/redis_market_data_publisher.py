import json
import time

import redis

from engine.live.data.redis_market_data_protocol import (
    CLOSED_CANDLES_STREAM,
    CLOSED_STREAM_MAXLEN,
    HISTORY_MAXLEN,
    PRICE_CHANNEL,
    history_key,
    last_closed_key,
    normalize_symbol,
    normalize_timeframe,
)


class RedisMarketDataPublisher:
    def __init__(
        self,
        host="127.0.0.1",
        port=6379,
        db=0,
        redis_client=None,
    ):
        self.redis = redis_client or redis.Redis(
            host=host,
            port=port,
            db=db,
            decode_responses=True,
        )

    def ping(self):
        return self.redis.ping()

    def replace_history(
        self,
        symbol: str,
        timeframe: str,
        candles: list[dict],
    ):
        symbol = normalize_symbol(symbol)
        timeframe = normalize_timeframe(timeframe)

        target_key = history_key(
            symbol,
            timeframe,
        )

        temporary_key = f"{target_key}:loading"

        normalized = [
            self._normalize_history_candle(
                symbol,
                timeframe,
                candle,
            )
            for candle in candles[-HISTORY_MAXLEN:]
        ]

        pipeline = self.redis.pipeline(
            transaction=True,
        )

        pipeline.delete(temporary_key)

        if normalized:
            pipeline.rpush(
                temporary_key,
                *[
                    self._serialize(candle)
                    for candle in normalized
                ],
            )

            pipeline.rename(
                temporary_key,
                target_key,
            )
        else:
            pipeline.delete(target_key)

        pipeline.execute()

        return len(normalized)

    def publish_ws_message(self, msg: dict):
        msg = self._unwrap_message(msg)

        if not isinstance(msg, dict):
            return None

        if msg.get("e") not in (
            "continuous_kline",
            "kline",
        ):
            return None

        kline = msg.get("k")

        if not isinstance(kline, dict):
            return None

        symbol = normalize_symbol(
            msg.get("s") or msg.get("ps")
        )

        timeframe = normalize_timeframe(
            kline.get("i")
        )

        event_timestamp = int(
            msg.get("E")
            or int(time.time() * 1000)
        )

        if timeframe == "1m":
            self._publish_price(
                symbol=symbol,
                price=float(kline["c"]),
                timestamp=event_timestamp,
            )

        if not kline.get("x"):
            return {
                "type": "price",
                "symbol": symbol,
                "timeframe": timeframe,
            }

        candle = self._normalize_ws_candle(
            symbol=symbol,
            timeframe=timeframe,
            kline=kline,
        )

        self._publish_closed_candle(candle)

        return {
            "type": "closed_candle",
            "symbol": symbol,
            "timeframe": timeframe,
            "timestamp": candle["timestamp"],
        }

    def _publish_price(
        self,
        symbol: str,
        price: float,
        timestamp: int,
    ):
        payload = {
            "type": "price",
            "symbol": symbol,
            "price": price,
            "timestamp": timestamp,
        }

        self.redis.publish(
            PRICE_CHANNEL,
            self._serialize(payload),
        )

    def _publish_closed_candle(
        self,
        candle: dict,
    ):
        symbol = candle["symbol"]
        timeframe = candle["timeframe"]

        serialized = self._serialize(candle)

        candle_history_key = history_key(
            symbol,
            timeframe,
        )

        last_history_item = self.redis.lindex(
            candle_history_key,
            -1,
        )

        replace_last = False

        if last_history_item:
            try:
                last_candle = json.loads(
                    last_history_item
                )

                replace_last = (
                    int(last_candle["timestamp"])
                    == int(candle["timestamp"])
                )

            except (
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                replace_last = False

        pipeline = self.redis.pipeline(
            transaction=True,
        )

        if replace_last:
            pipeline.lset(
                candle_history_key,
                -1,
                serialized,
            )
        else:
            pipeline.rpush(
                candle_history_key,
                serialized,
            )

        pipeline.ltrim(
            candle_history_key,
            -HISTORY_MAXLEN,
            -1,
        )

        pipeline.set(
            last_closed_key(
                symbol,
                timeframe,
            ),
            serialized,
        )

        pipeline.xadd(
            CLOSED_CANDLES_STREAM,
            {
                "payload": serialized,
            },
            maxlen=CLOSED_STREAM_MAXLEN,
            approximate=True,
        )

        pipeline.execute()

    def _normalize_history_candle(
        self,
        symbol: str,
        timeframe: str,
        candle: dict,
    ):
        timestamp = candle["timestamp"]

        if hasattr(timestamp, "timestamp"):
            timestamp = int(
                timestamp.timestamp() * 1000
            )
        else:
            timestamp = int(timestamp)

        normalized = {
            "type": "closed_candle",
            "symbol": symbol,
            "timeframe": timeframe,
            "timestamp": timestamp,
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
            "volume": float(candle["volume"]),
        }

        quote_volume = (
            candle.get("quoteVolume")
            or candle.get("quote_volume")
            or candle.get("quote_asset_volume")
        )

        if quote_volume is not None:
            normalized["quoteVolume"] = float(
                quote_volume
            )

        return normalized

    def _normalize_ws_candle(
        self,
        symbol: str,
        timeframe: str,
        kline: dict,
    ):
        return {
            "type": "closed_candle",
            "symbol": symbol,
            "timeframe": timeframe,
            "timestamp": int(kline["t"]),
            "close_timestamp": int(kline["T"]),
            "open": float(kline["o"]),
            "high": float(kline["h"]),
            "low": float(kline["l"]),
            "close": float(kline["c"]),
            "volume": float(kline["v"]),
            "quoteVolume": float(
                kline.get("q", 0)
            ),
        }

    def _unwrap_message(self, msg):
        if (
            isinstance(msg, dict)
            and "data" in msg
        ):
            return msg["data"]

        return msg

    def _serialize(self, value):
        return json.dumps(
            value,
            separators=(",", ":"),
        )