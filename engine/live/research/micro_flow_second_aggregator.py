import copy
import threading


class MicroFlowSecondAggregator:
    """Aggregate Binance USD-M aggTrade messages into completed 1-second bars.

    The class is intentionally transport-agnostic. It receives raw/combined
    websocket messages, keeps only the current second per symbol, and emits a
    completed state after a small grace period. Resting order-book liquidity is
    not used: BUY/SELL flow is inferred only from executed aggregate trades.
    """

    def __init__(self, finalize_grace_ms=750):
        self.finalize_grace_ms = max(0, int(finalize_grace_ms))
        self._lock = threading.Lock()
        self._current = {}
        self._last_finalized_second = {}

        self.agg_trades_received = 0
        self.seconds_finalized = 0
        self.late_trades_dropped = 0
        self.invalid_messages = 0

    @staticmethod
    def _unwrap(message):
        if isinstance(message, dict) and isinstance(message.get("data"), dict):
            return message["data"]
        return message

    @classmethod
    def is_agg_trade_message(cls, message):
        payload = cls._unwrap(message)
        return isinstance(payload, dict) and payload.get("e") == "aggTrade"

    @staticmethod
    def _new_bucket(
        symbol,
        second_ms,
        trade_timestamp,
        agg_trade_id,
        price,
        quantity,
        buyer_is_maker,
    ):
        notional = float(price) * float(quantity)
        aggressive_buy = not bool(buyer_is_maker)

        return {
            "type": "micro_flow_second",
            "symbol": str(symbol).upper(),
            "timestamp": int(second_ms),
            "open": float(price),
            "high": float(price),
            "low": float(price),
            "close": float(price),
            "trade_count": 1,
            "buy_trades": 1 if aggressive_buy else 0,
            "sell_trades": 0 if aggressive_buy else 1,
            "buy_notional": notional if aggressive_buy else 0.0,
            "sell_notional": 0.0 if aggressive_buy else notional,
            "total_notional": notional,
            "first_agg_trade_id": int(agg_trade_id),
            "last_agg_trade_id": int(agg_trade_id),
            "first_trade_timestamp": int(trade_timestamp),
            "last_trade_timestamp": int(trade_timestamp),
        }

    @staticmethod
    def _update_bucket(
        bucket,
        trade_timestamp,
        agg_trade_id,
        price,
        quantity,
        buyer_is_maker,
    ):
        price = float(price)
        quantity = float(quantity)
        notional = price * quantity
        aggressive_buy = not bool(buyer_is_maker)

        bucket["high"] = max(float(bucket["high"]), price)
        bucket["low"] = min(float(bucket["low"]), price)
        bucket["close"] = price
        bucket["trade_count"] += 1
        bucket["total_notional"] += notional

        if aggressive_buy:
            bucket["buy_trades"] += 1
            bucket["buy_notional"] += notional
        else:
            bucket["sell_trades"] += 1
            bucket["sell_notional"] += notional

        bucket["last_agg_trade_id"] = int(agg_trade_id)
        bucket["last_trade_timestamp"] = int(trade_timestamp)

    @staticmethod
    def _finalize(bucket):
        if bucket is None:
            return None

        result = copy.deepcopy(bucket)
        total = float(result.get("total_notional", 0.0))
        buy = float(result.get("buy_notional", 0.0))
        sell = float(result.get("sell_notional", 0.0))

        result["flow_imbalance_1s"] = (
            (buy - sell) / total
            if total > 0
            else 0.0
        )
        result["close_timestamp"] = int(result["timestamp"]) + 999
        return result

    def ingest_ws_message(self, message):
        """Ingest one WS message and return any seconds finalized by rollover."""
        payload = self._unwrap(message)

        if not isinstance(payload, dict) or payload.get("e") != "aggTrade":
            return []

        try:
            symbol = str(payload["s"]).upper()
            agg_trade_id = int(payload["a"])
            price = float(payload["p"])
            quantity = float(payload["q"])
            trade_timestamp = int(payload["T"])
            buyer_is_maker = bool(payload["m"])
        except (KeyError, TypeError, ValueError):
            with self._lock:
                self.invalid_messages += 1
            return []

        second_ms = (trade_timestamp // 1000) * 1000
        finalized = []

        with self._lock:
            self.agg_trades_received += 1

            last_finalized = self._last_finalized_second.get(symbol)
            if last_finalized is not None and second_ms <= last_finalized:
                self.late_trades_dropped += 1
                return []

            current = self._current.get(symbol)

            if current is None:
                self._current[symbol] = self._new_bucket(
                    symbol=symbol,
                    second_ms=second_ms,
                    trade_timestamp=trade_timestamp,
                    agg_trade_id=agg_trade_id,
                    price=price,
                    quantity=quantity,
                    buyer_is_maker=buyer_is_maker,
                )
                return []

            current_second = int(current["timestamp"])

            if second_ms < current_second:
                # Per-symbol Binance aggTrade streams should be ordered. If an
                # older message arrives after the current second started, do not
                # revise already-aggregating state; count it as a late drop.
                self.late_trades_dropped += 1
                return []

            if second_ms > current_second:
                completed = self._finalize(current)
                if completed is not None:
                    finalized.append(completed)
                    self._last_finalized_second[symbol] = current_second
                    self.seconds_finalized += 1

                self._current[symbol] = self._new_bucket(
                    symbol=symbol,
                    second_ms=second_ms,
                    trade_timestamp=trade_timestamp,
                    agg_trade_id=agg_trade_id,
                    price=price,
                    quantity=quantity,
                    buyer_is_maker=buyer_is_maker,
                )
                return finalized

            self._update_bucket(
                current,
                trade_timestamp=trade_timestamp,
                agg_trade_id=agg_trade_id,
                price=price,
                quantity=quantity,
                buyer_is_maker=buyer_is_maker,
            )

        return finalized

    def flush_ready(self, now_ms):
        """Finalize active buckets whose second ended plus grace period."""
        now_ms = int(now_ms)
        finalized = []

        with self._lock:
            ready_symbols = []

            for symbol, bucket in self._current.items():
                second_ms = int(bucket["timestamp"])
                ready_at = second_ms + 1000 + self.finalize_grace_ms
                if now_ms >= ready_at:
                    ready_symbols.append(symbol)

            for symbol in ready_symbols:
                bucket = self._current.pop(symbol, None)
                if bucket is None:
                    continue

                completed = self._finalize(bucket)
                if completed is None:
                    continue

                second_ms = int(completed["timestamp"])
                last_finalized = self._last_finalized_second.get(symbol)
                if last_finalized is not None and second_ms <= last_finalized:
                    continue

                self._last_finalized_second[symbol] = second_ms
                self.seconds_finalized += 1
                finalized.append(completed)

        return finalized

    def diagnostics(self):
        with self._lock:
            return {
                "agg_trades_received": int(self.agg_trades_received),
                "seconds_finalized": int(self.seconds_finalized),
                "late_trades_dropped": int(self.late_trades_dropped),
                "invalid_messages": int(self.invalid_messages),
                "active_symbol_buckets": int(len(self._current)),
                "finalize_grace_ms": int(self.finalize_grace_ms),
            }
