import csv
from pathlib import Path

from engine.live.research.swing_detector import SwingDetector


MINUTE_MS = 60_000


class ConfirmedSwingSweepReplayStrategy:
    """Research-only causal replay for confirmed 15m swing sweeps.

    Frozen V1 hypothesis:
      - symmetric confirmed swing on 15m (default 5x5)
      - swing becomes actionable only after the confirming 15m candle closes
      - require >= 0.20% departure from the swing
      - departure candle itself can never also count as the sweep
      - only the FIRST later sweep is eligible
      - resting LIMIT is 0.05% inside the swing
      - LONG/SHORT are exact mirrors
      - TP 0.75%, SL 1.00%, max hold 180 full 1m candles after fill candle
      - entry-candle SL is credited conservatively; entry-candle TP is never
        credited because OHLC does not prove it happened after the fill
      - on later candles TP+SL together => SL_AMBIGUOUS

    This module deliberately does NOT use ExecutionEngine position limits.
    Every setup is an independent research observation, matching the dashboard
    research semantics.
    """

    EVENT_FIELDS = [
        "transition",
        "setup_id",
        "symbol",
        "side",
        "swing_timeframe",
        "detector",
        "pivot_timestamp",
        "confirmed_timestamp",
        "actionable_timestamp",
        "swing_price",
        "prominence_pct",
        "confirmation_close",
        "pivot_to_confirmation_pct",
        "departure_timestamp",
        "departure_price",
        "max_departure_pct",
        "sweep_timestamp",
        "sweep_open",
        "sweep_high",
        "sweep_low",
        "sweep_close",
        "sweep_close_location",
        "sweep_penetration_pct",
        "confirmed_to_sweep_min",
        "limit_price",
        "filled",
        "reason",
    ]

    TRADE_FIELDS = [
        "setup_id",
        "symbol",
        "side",
        "swing_timeframe",
        "detector",
        "pivot_timestamp",
        "confirmed_timestamp",
        "actionable_timestamp",
        "swing_price",
        "prominence_pct",
        "confirmation_close",
        "pivot_to_confirmation_pct",
        "departure_timestamp",
        "departure_price",
        "max_departure_pct",
        "sweep_timestamp",
        "sweep_penetration_pct",
        "sweep_close_location",
        "entry_timestamp",
        "entry_price",
        "tp_price",
        "sl_price",
        "exit_timestamp",
        "exit_price",
        "exit_reason",
        "bars_after_entry",
        "reclaim_timestamp",
        "sweep_to_reclaim_min",
        "mfe_pct",
        "mae_pct",
        "gross_return_pct",
        "fee_per_side_pct",
        "fees_pct",
        "net_return_pct",
        "complete",
        "ambiguous",
    ]

    def __init__(
        self,
        buffer,
        events_path="replay-artifacts/confirmed_swing_sweep_v1_events.csv",
        trades_path="replay-artifacts/confirmed_swing_sweep_v1_trades.csv",
        swing_timeframe="15m",
        left_bars=5,
        right_bars=5,
        min_prominence_pct=0.0,
        min_departure_pct=0.20,
        max_sweep_age_minutes=360,
        entry_offset_pct=0.05,
        tp_pct=0.75,
        sl_pct=1.00,
        max_hold_bars=180,
        fee_per_side_pct=0.05,
        reset_output=True,
        detector=None,
    ):
        self.buffer = buffer

        self.events_path = Path(events_path)
        self.trades_path = Path(trades_path)

        self.swing_timeframe = str(swing_timeframe)
        if self.swing_timeframe != "15m":
            raise ValueError("Sweep replay V1 is frozen to swing_timeframe=15m")

        self.left_bars = int(left_bars)
        self.right_bars = int(right_bars)
        self.min_prominence_pct = float(min_prominence_pct)
        self.min_departure_pct = float(min_departure_pct)
        self.max_sweep_age_minutes = int(max_sweep_age_minutes)
        self.entry_offset_pct = float(entry_offset_pct)
        self.tp_pct = float(tp_pct)
        self.sl_pct = float(sl_pct)
        self.max_hold_bars = int(max_hold_bars)
        self.fee_per_side_pct = float(fee_per_side_pct)

        if self.left_bars < 1 or self.right_bars < 1:
            raise ValueError("left_bars/right_bars must be >= 1")
        if self.min_prominence_pct < 0:
            raise ValueError("min_prominence_pct must be >= 0")
        if self.min_departure_pct < 0:
            raise ValueError("min_departure_pct must be >= 0")
        if self.max_sweep_age_minutes < 1:
            raise ValueError("max_sweep_age_minutes must be >= 1")
        if self.entry_offset_pct < 0:
            raise ValueError("entry_offset_pct must be >= 0")
        if self.tp_pct <= 0 or self.sl_pct <= 0:
            raise ValueError("tp_pct/sl_pct must be > 0")
        if self.max_hold_bars < 1:
            raise ValueError("max_hold_bars must be >= 1")
        if self.fee_per_side_pct < 0:
            raise ValueError("fee_per_side_pct must be >= 0")

        self.detector = detector or SwingDetector(
            left_bars=self.left_bars,
            right_bars=self.right_bars,
            min_prominence_pct=self.min_prominence_pct,
        )

        # symbol -> setup_id -> setup state
        self.active_setups = {}

        # symbol -> setup_id -> independent virtual position
        self.positions = {}

        # A pivot is never registered twice, even if detect_all returns it on
        # every subsequent 1m candle.
        self.known_setup_ids = set()

        self.last_processed_open_ts = {}
        self.first_processed_open_ts = {}

        self.stats = {
            "setups": 0,
            "departures": 0,
            "first_sweeps": 0,
            "fills": 0,
            "no_fills": 0,
            "expired": 0,
            "setup_gaps": 0,
            "trade_gaps": 0,
            "trades": 0,
            "TP": 0,
            "SL": 0,
            "SL_AMBIGUOUS": 0,
            "TIME_EXIT": 0,
        }
        self.closed_trade_returns = []

        for path in (self.events_path, self.trades_path):
            path.parent.mkdir(parents=True, exist_ok=True)
            if reset_output and path.exists():
                path.unlink()

        print(
            "[SWEEP REPLAY V1] enabled | "
            f"swing={self.swing_timeframe} "
            f"detector={self.left_bars}x{self.right_bars} "
            f"prom>={self.min_prominence_pct:.3f}% "
            f"departure>={self.min_departure_pct:.3f}% "
            f"max_age={self.max_sweep_age_minutes}m "
            f"offset={self.entry_offset_pct:.3f}% "
            f"TP={self.tp_pct:.3f}% "
            f"SL={self.sl_pct:.3f}% "
            f"hold={self.max_hold_bars}m "
            f"fee/side={self.fee_per_side_pct:.3f}%"
        )

    @property
    def detector_name(self):
        return f"{self.left_bars}x{self.right_bars}"

    @staticmethod
    def _close_location(candle):
        high = float(candle["high"])
        low = float(candle["low"])
        close = float(candle["close"])
        span = max(0.0, high - low)
        if span <= 0:
            return 0.5
        return (close - low) / span

    def _setup_id(self, symbol, point):
        return (
            f"{str(symbol).upper()}|{str(point.side).upper()}|"
            f"{int(point.pivot_timestamp)}|{int(point.confirmed_timestamp)}"
        )

    def _confirmation_context(self, symbol, point):
        candles = self.buffer.get_candles(symbol, self.swing_timeframe) or []
        target_ts = int(point.confirmed_timestamp)

        confirmation_close = None
        for candle in reversed(candles):
            if int(candle["timestamp"]) == target_ts:
                confirmation_close = float(candle["close"])
                break

        pivot_price = float(point.price)
        pivot_to_confirmation_pct = None

        if confirmation_close is not None and pivot_price > 0:
            if str(point.side).upper() == "LOW":
                pivot_to_confirmation_pct = (
                    confirmation_close / pivot_price - 1.0
                ) * 100.0
            else:
                pivot_to_confirmation_pct = (
                    pivot_price / confirmation_close - 1.0
                ) * 100.0

        return confirmation_close, pivot_to_confirmation_pct

    def _new_setup_state(self, symbol, point):
        side = "LONG" if str(point.side).upper() == "LOW" else "SHORT"
        confirmed_ts = int(point.confirmed_timestamp)
        actionable_ts = confirmed_ts + 15 * MINUTE_MS
        confirmation_close, pivot_to_confirmation_pct = (
            self._confirmation_context(symbol, point)
        )

        return {
            "setup_id": self._setup_id(symbol, point),
            "symbol": str(symbol).upper(),
            "side": side,
            "swing_timeframe": self.swing_timeframe,
            "detector": self.detector_name,
            "pivot_timestamp": int(point.pivot_timestamp),
            "confirmed_timestamp": confirmed_ts,
            "actionable_timestamp": actionable_ts,
            "swing_price": float(point.price),
            "prominence_pct": getattr(point, "prominence_pct", None),
            "confirmation_close": confirmation_close,
            "pivot_to_confirmation_pct": pivot_to_confirmation_pct,
            "departure_timestamp": None,
            "departure_price": None,
            "max_departure_pct": 0.0,
        }

    def _append_csv(self, path, fieldnames, row):
        exists = path.exists()
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=fieldnames,
                extrasaction="ignore",
            )
            if not exists:
                writer.writeheader()
            writer.writerow({key: row.get(key) for key in fieldnames})

    def _journal_event(self, setup, transition, **extra):
        row = {key: None for key in self.EVENT_FIELDS}
        row.update(setup)
        row.update(extra)
        row["transition"] = transition
        self._append_csv(self.events_path, self.EVENT_FIELDS, row)

    def _active_setup_map(self, symbol):
        return self.active_setups.setdefault(str(symbol).upper(), {})

    def _position_map(self, symbol):
        return self.positions.setdefault(str(symbol).upper(), {})

    def _drop_empty_symbol_maps(self, symbol):
        symbol = str(symbol).upper()
        if not self.active_setups.get(symbol):
            self.active_setups.pop(symbol, None)
        if not self.positions.get(symbol):
            self.positions.pop(symbol, None)

    def _bootstrap_setup(self, setup, before_open_ts):
        """Reconstruct pre-replay state from trusted 1m history.

        Returns True only when the setup is still eligible at before_open_ts.
        Any prior FIRST sweep consumes the setup and is intentionally excluded
        from the scored replay because its fill happened before our replay
        observation window.
        """
        actionable_ts = int(setup["actionable_timestamp"])
        if actionable_ts >= int(before_open_ts):
            return True

        candles = self.buffer.get_candles(setup["symbol"], "1m") or []
        history = sorted(
            (
                candle
                for candle in candles
                if actionable_ts <= int(candle["timestamp"]) < int(before_open_ts)
            ),
            key=lambda item: int(item["timestamp"]),
        )

        if not history:
            return False
        if int(history[0]["timestamp"]) != actionable_ts:
            return False

        expected = actionable_ts
        for candle in history:
            candle_ts = int(candle["timestamp"])
            if candle_ts != expected:
                return False
            expected += MINUTE_MS

            result = self._advance_setup_state(
                setup,
                candle,
                score=False,
            )
            if result in {"CONSUMED", "EXPIRED"}:
                return False

        return True

    def _discover_new_swings(self, symbol, current_open_ts):
        symbol = str(symbol).upper()
        candles = self.buffer.get_candles(symbol, self.swing_timeframe) or []
        if not candles:
            return

        points = self.detector.detect_all(candles)

        for point in points:
            point_side = str(point.side).upper()
            if point_side not in {"LOW", "HIGH"}:
                continue

            setup_id = self._setup_id(symbol, point)
            if setup_id in self.known_setup_ids:
                continue

            setup = self._new_setup_state(symbol, point)
            actionable_ts = int(setup["actionable_timestamp"])

            # Not yet available. Do not mark known; it will be reconsidered.
            if actionable_ts > int(current_open_ts):
                continue

            self.known_setup_ids.add(setup_id)

            # If the entire eligibility window ended before this replay candle,
            # it cannot create a scored event here.
            deadline = (
                actionable_ts
                + self.max_sweep_age_minutes * MINUTE_MS
            )
            if int(current_open_ts) > deadline:
                continue

            if actionable_ts < int(current_open_ts):
                if not self._bootstrap_setup(setup, current_open_ts):
                    continue

            self._active_setup_map(symbol)[setup_id] = setup
            self.stats["setups"] += 1
            self._journal_event(setup, "SETUP_ACTIVE")

    def _away_pct(self, setup, candle):
        swing_price = float(setup["swing_price"])
        if setup["side"] == "LONG":
            return max(
                0.0,
                (float(candle["high"]) / swing_price - 1.0) * 100.0,
            )
        return max(
            0.0,
            (1.0 - float(candle["low"]) / swing_price) * 100.0,
        )

    def _sweep_penetration(self, setup, candle):
        swing_price = float(setup["swing_price"])
        if setup["side"] == "LONG":
            low = float(candle["low"])
            if low >= swing_price:
                return None
            return max(0.0, (1.0 - low / swing_price) * 100.0)

        high = float(candle["high"])
        if high <= swing_price:
            return None
        return max(0.0, (high / swing_price - 1.0) * 100.0)

    def _limit_price(self, setup):
        swing_price = float(setup["swing_price"])
        if setup["side"] == "LONG":
            return swing_price * (1.0 - self.entry_offset_pct / 100.0)
        return swing_price * (1.0 + self.entry_offset_pct / 100.0)

    def _advance_setup_state(self, setup, candle, score=True):
        candle_ts = int(candle["timestamp"])
        actionable_ts = int(setup["actionable_timestamp"])
        deadline = actionable_ts + self.max_sweep_age_minutes * MINUTE_MS

        if candle_ts < actionable_ts:
            return "ACTIVE"

        if candle_ts > deadline:
            if score:
                self.stats["expired"] += 1
                self._journal_event(setup, "EXPIRED", reason="MAX_SWEEP_AGE")
            return "EXPIRED"

        away_pct = self._away_pct(setup, candle)
        setup["max_departure_pct"] = max(
            float(setup.get("max_departure_pct") or 0.0),
            float(away_pct),
        )

        if setup.get("departure_timestamp") is None:
            if away_pct >= self.min_departure_pct:
                setup["departure_timestamp"] = candle_ts
                setup["departure_price"] = (
                    float(candle["high"])
                    if setup["side"] == "LONG"
                    else float(candle["low"])
                )
                if score:
                    self.stats["departures"] += 1
                    self._journal_event(setup, "DEPARTURE")

                # Exact dashboard rule: departure and sweep on the same 1m
                # candle are not ordered from OHLC, so this candle cannot sweep.
                return "ACTIVE"
            return "ACTIVE"

        if candle_ts <= int(setup["departure_timestamp"]):
            return "ACTIVE"

        penetration_pct = self._sweep_penetration(setup, candle)
        if penetration_pct is None:
            return "ACTIVE"

        # FIRST sweep consumes the setup whether or not the deeper limit fills.
        if not score:
            return "CONSUMED"

        self.stats["first_sweeps"] += 1
        limit_price = self._limit_price(setup)
        filled = penetration_pct + 1e-12 >= self.entry_offset_pct
        sweep_close_location = self._close_location(candle)

        sweep_fields = {
            "sweep_timestamp": candle_ts,
            "sweep_open": float(candle["open"]),
            "sweep_high": float(candle["high"]),
            "sweep_low": float(candle["low"]),
            "sweep_close": float(candle["close"]),
            "sweep_close_location": float(sweep_close_location),
            "sweep_penetration_pct": float(penetration_pct),
            "confirmed_to_sweep_min": (
                (candle_ts - actionable_ts) / MINUTE_MS
            ),
            "limit_price": float(limit_price),
            "filled": bool(filled),
        }

        if not filled:
            self.stats["no_fills"] += 1
            self._journal_event(
                setup,
                "FIRST_SWEEP_NO_FILL",
                reason="FIRST_SWEEP_DID_NOT_REACH_LIMIT",
                **sweep_fields,
            )
            return "CONSUMED"

        self.stats["fills"] += 1
        self._journal_event(setup, "FIRST_SWEEP_FILLED", **sweep_fields)
        self._open_position(setup, candle, penetration_pct, sweep_close_location)
        return "CONSUMED"

    def _open_position(self, setup, sweep_candle, penetration_pct, close_location):
        symbol = setup["symbol"]
        setup_id = setup["setup_id"]
        entry_price = self._limit_price(setup)
        sweep_ts = int(sweep_candle["timestamp"])

        if setup["side"] == "LONG":
            tp_price = entry_price * (1.0 + self.tp_pct / 100.0)
            sl_price = entry_price * (1.0 - self.sl_pct / 100.0)
            immediate_sl = float(sweep_candle["low"]) <= sl_price
            immediate_adverse = max(
                0.0,
                (1.0 - float(sweep_candle["low"]) / entry_price) * 100.0,
            )
            reclaimed_same_candle = float(sweep_candle["close"]) >= float(
                setup["swing_price"]
            )
        else:
            tp_price = entry_price * (1.0 - self.tp_pct / 100.0)
            sl_price = entry_price * (1.0 + self.sl_pct / 100.0)
            immediate_sl = float(sweep_candle["high"]) >= sl_price
            immediate_adverse = max(
                0.0,
                (float(sweep_candle["high"]) / entry_price - 1.0) * 100.0,
            )
            reclaimed_same_candle = float(sweep_candle["close"]) <= float(
                setup["swing_price"]
            )

        position = {
            **setup,
            "sweep_timestamp": sweep_ts,
            "sweep_penetration_pct": float(penetration_pct),
            "sweep_close_location": float(close_location),
            "entry_timestamp": sweep_ts,
            "entry_price": float(entry_price),
            "tp_price": float(tp_price),
            "sl_price": float(sl_price),
            "bars_after_entry": 0,
            "mfe_pct": 0.0,
            "mae_pct": float(immediate_adverse),
            "reclaim_timestamp": sweep_ts if reclaimed_same_candle else None,
        }

        if immediate_sl:
            self._close_position_state(
                position,
                exit_timestamp=sweep_ts,
                exit_price=sl_price,
                exit_reason="SL",
                ambiguous=False,
            )
            return

        self._position_map(symbol)[setup_id] = position

    def _update_reclaim(self, position, candle):
        if position.get("reclaim_timestamp") is not None:
            return

        swing_price = float(position["swing_price"])
        close = float(candle["close"])
        reclaimed = (
            close >= swing_price
            if position["side"] == "LONG"
            else close <= swing_price
        )
        if reclaimed:
            position["reclaim_timestamp"] = int(candle["timestamp"])

    def _process_positions(self, symbol, candle):
        symbol = str(symbol).upper()
        positions = self.positions.get(symbol)
        if not positions:
            return

        candle_ts = int(candle["timestamp"])
        high = float(candle["high"])
        low = float(candle["low"])
        close = float(candle["close"])

        for setup_id, position in list(positions.items()):
            # Fill candle is handled separately when opening the position.
            if candle_ts <= int(position["entry_timestamp"]):
                continue

            position["bars_after_entry"] += 1
            self._update_reclaim(position, candle)

            entry = float(position["entry_price"])
            if position["side"] == "LONG":
                favorable = max(0.0, (high / entry - 1.0) * 100.0)
                adverse = max(0.0, (1.0 - low / entry) * 100.0)
                tp_hit = high >= float(position["tp_price"])
                sl_hit = low <= float(position["sl_price"])
            else:
                favorable = max(0.0, (1.0 - low / entry) * 100.0)
                adverse = max(0.0, (high / entry - 1.0) * 100.0)
                tp_hit = low <= float(position["tp_price"])
                sl_hit = high >= float(position["sl_price"])

            position["mfe_pct"] = max(float(position["mfe_pct"]), favorable)
            position["mae_pct"] = max(float(position["mae_pct"]), adverse)

            if tp_hit and sl_hit:
                self._close_position_state(
                    position,
                    exit_timestamp=candle_ts,
                    exit_price=float(position["sl_price"]),
                    exit_reason="SL_AMBIGUOUS",
                    ambiguous=True,
                )
                positions.pop(setup_id, None)
                continue

            if sl_hit:
                self._close_position_state(
                    position,
                    exit_timestamp=candle_ts,
                    exit_price=float(position["sl_price"]),
                    exit_reason="SL",
                    ambiguous=False,
                )
                positions.pop(setup_id, None)
                continue

            if tp_hit:
                self._close_position_state(
                    position,
                    exit_timestamp=candle_ts,
                    exit_price=float(position["tp_price"]),
                    exit_reason="TP",
                    ambiguous=False,
                )
                positions.pop(setup_id, None)
                continue

            if int(position["bars_after_entry"]) >= self.max_hold_bars:
                self._close_position_state(
                    position,
                    exit_timestamp=candle_ts,
                    exit_price=close,
                    exit_reason="TIME_EXIT",
                    ambiguous=False,
                )
                positions.pop(setup_id, None)

        self._drop_empty_symbol_maps(symbol)

    def _close_position_state(
        self,
        position,
        exit_timestamp,
        exit_price,
        exit_reason,
        ambiguous,
    ):
        entry = float(position["entry_price"])
        exit_price = float(exit_price)

        if exit_reason == "TP":
            gross_pct = self.tp_pct
        elif exit_reason in {"SL", "SL_AMBIGUOUS"}:
            gross_pct = -self.sl_pct
        elif position["side"] == "LONG":
            gross_pct = (exit_price / entry - 1.0) * 100.0
        else:
            gross_pct = (1.0 - exit_price / entry) * 100.0

        fees_pct = self.fee_per_side_pct * 2.0
        net_pct = float(gross_pct) - float(fees_pct)

        reclaim_ts = position.get("reclaim_timestamp")
        sweep_to_reclaim_min = None
        if reclaim_ts is not None:
            sweep_to_reclaim_min = (
                int(reclaim_ts) - int(position["sweep_timestamp"])
            ) / MINUTE_MS

        row = {
            **position,
            "exit_timestamp": int(exit_timestamp),
            "exit_price": float(exit_price),
            "exit_reason": str(exit_reason),
            "sweep_to_reclaim_min": sweep_to_reclaim_min,
            "gross_return_pct": float(gross_pct),
            "fee_per_side_pct": float(self.fee_per_side_pct),
            "fees_pct": float(fees_pct),
            "net_return_pct": float(net_pct),
            "complete": True,
            "ambiguous": bool(ambiguous),
        }

        self._append_csv(self.trades_path, self.TRADE_FIELDS, row)
        self.stats["trades"] += 1
        self.stats[str(exit_reason)] = self.stats.get(str(exit_reason), 0) + 1
        self.closed_trade_returns.append(float(net_pct))

        print(
            "[SWEEP REPLAY EXIT] "
            f"symbol={position['symbol']} "
            f"side={position['side']} "
            f"reason={exit_reason} "
            f"entry={entry:.8f} "
            f"exit={exit_price:.8f} "
            f"net={net_pct:.4f}% "
            f"bars={position['bars_after_entry']}"
        )

    def _invalidate_on_gap(self, symbol, current_open_ts, previous_open_ts):
        symbol = str(symbol).upper()

        for setup in list(self.active_setups.get(symbol, {}).values()):
            self.stats["setup_gaps"] += 1
            self._journal_event(
                setup,
                "SETUP_GAP_EXPIRED",
                reason=(
                    f"1M_GAP_{int(previous_open_ts)}_TO_{int(current_open_ts)}"
                ),
            )

        for position in list(self.positions.get(symbol, {}).values()):
            self.stats["trade_gaps"] += 1
            self._journal_event(
                position,
                "TRADE_GAP_INCOMPLETE",
                filled=True,
                reason=(
                    f"1M_GAP_{int(previous_open_ts)}_TO_{int(current_open_ts)}"
                ),
            )

        self.active_setups.pop(symbol, None)
        self.positions.pop(symbol, None)

    def on_candle(self, symbol, close_time, candle):
        """Process one CLOSED 1m candle, exactly once per symbol/open timestamp."""
        if candle is None:
            return

        symbol = str(symbol).upper()
        close_time = int(close_time)
        candle_open_ts = int(candle["timestamp"])

        last_open = self.last_processed_open_ts.get(symbol)
        if last_open is not None:
            if candle_open_ts <= int(last_open):
                return
            if candle_open_ts != int(last_open) + MINUTE_MS:
                self._invalidate_on_gap(symbol, candle_open_ts, last_open)

        self.last_processed_open_ts[symbol] = candle_open_ts
        self.first_processed_open_ts.setdefault(symbol, candle_open_ts)

        # Existing trades see this candle before a new setup can fill on it.
        self._process_positions(symbol, candle)

        # 15m closed data for this replay boundary is already in DataBuffer by
        # the time main.py processes the 1m context boundary.
        self._discover_new_swings(symbol, candle_open_ts)

        setups = self.active_setups.get(symbol)
        if not setups:
            return

        for setup_id, setup in list(setups.items()):
            result = self._advance_setup_state(setup, candle, score=True)
            if result in {"CONSUMED", "EXPIRED"}:
                setups.pop(setup_id, None)

        self._drop_empty_symbol_maps(symbol)

    def summary(self):
        returns = list(self.closed_trade_returns)
        n = len(returns)

        if not returns:
            return {
                **self.stats,
                "win_rate_pct": None,
                "avg_net_pct": None,
                "total_net_pct": 0.0,
                "profit_factor": None,
            }

        positive = sum(value for value in returns if value > 0)
        negative = sum(value for value in returns if value < 0)
        pf = (
            positive / abs(negative)
            if negative < 0
            else (float("inf") if positive > 0 else None)
        )

        return {
            **self.stats,
            "win_rate_pct": sum(value > 0 for value in returns) / n * 100.0,
            "avg_net_pct": sum(returns) / n,
            "total_net_pct": sum(returns),
            "profit_factor": pf,
        }

    def print_summary(self):
        s = self.summary()
        print(
            "[SWEEP REPLAY SUMMARY] "
            f"setups={s['setups']} "
            f"departures={s['departures']} "
            f"first_sweeps={s['first_sweeps']} "
            f"fills={s['fills']} "
            f"no_fills={s['no_fills']} "
            f"trades={s['trades']} "
            f"TP={s['TP']} "
            f"SL={s['SL']} "
            f"SL_AMBIGUOUS={s['SL_AMBIGUOUS']} "
            f"TIME_EXIT={s['TIME_EXIT']} "
            f"WR={s['win_rate_pct']} "
            f"avg_net={s['avg_net_pct']} "
            f"PF={s['profit_factor']} "
            f"total_net={s['total_net_pct']}"
        )
