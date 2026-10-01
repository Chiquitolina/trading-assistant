import csv
from pathlib import Path


MINUTE_MS = 60_000


class VolumeExhaustionReplayStrategy:

    def __init__(
        self,
        buffer,
        trades_path=(
            "volume_exhaustion_replay_trades.csv"
        ),
        rsi_max=25.0,
        relative_volume_min=2.5,
        volume_3m_min=2.0,
        hv5_min=2,
        move_3m_max=-0.75,
        running_low_lookback=30,
        max_distance_low_pct=0.15,
        confirmation_bars=3,
        tp_pct=0.75,
        sl_pct=0.50,
        max_hold_bars=30,
        notional_usdt=100.0,
        taker_fee_pct=0.05,
        reset_output=True,
    ):
        self.buffer = buffer
        self.trades_path = Path(
            trades_path
        )

        self.rsi_max = float(
            rsi_max
        )
        self.relative_volume_min = float(
            relative_volume_min
        )
        self.volume_3m_min = float(
            volume_3m_min
        )
        self.hv5_min = int(
            hv5_min
        )
        self.move_3m_max = float(
            move_3m_max
        )

        self.running_low_lookback = int(
            running_low_lookback
        )
        self.max_distance_low_pct = float(
            max_distance_low_pct
        )

        self.confirmation_bars = int(
            confirmation_bars
        )

        self.tp_pct = float(
            tp_pct
        )
        self.sl_pct = float(
            sl_pct
        )

        self.max_hold_bars = int(
            max_hold_bars
        )

        self.notional_usdt = float(
            notional_usdt
        )

        self.taker_fee_pct = float(
            taker_fee_pct
        )

        self.waiting = {}
        self.pending_entries = {}
        self.positions = {}

        self.trades = []

        self.last_processed_close = {}

        self.detected = 0
        self.confirmed = 0
        self.expired = 0

        self.trades_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        if (
            reset_output
            and self.trades_path.exists()
        ):
            self.trades_path.unlink()

        print(
            "[VE REPLAY STRATEGY] enabled | "
            f"rsi<={self.rsi_max} "
            f"vol>={self.relative_volume_min}x "
            f"vol3m>={self.volume_3m_min}x "
            f"hv5>={self.hv5_min} "
            f"move3m<={self.move_3m_max}% "
            f"low_dist<={self.max_distance_low_pct}% "
            f"confirm={self.confirmation_bars} "
            f"TP={self.tp_pct}% "
            f"SL={self.sl_pct}% "
            f"hold={self.max_hold_bars}m"
        )

    # =====================================================
    # RUNNING LOW
    # =====================================================

    def _running_low_context(
        self,
        event,
    ):
        symbol = event["symbol"]

        event_open_ts = int(
            event["candle_open_timestamp"]
        )

        candles = self.buffer.get_candles(
            symbol,
            "1m",
        )

        eligible = [
            candle
            for candle in candles
            if int(candle["timestamp"])
            <= event_open_ts
        ]

        if (
            len(eligible)
            < self.running_low_lookback
        ):
            return None, None

        window = eligible[
            -self.running_low_lookback:
        ]

        running_low = min(
            float(candle["low"])
            for candle in window
        )

        close = float(
            event["close"]
        )

        if running_low <= 0:
            return None, None

        distance_pct = (
            (close / running_low) - 1.0
        ) * 100.0

        return (
            running_low,
            distance_pct,
        )

    # =====================================================
    # DETECTION
    # =====================================================

    def on_event(
        self,
        event,
    ):
        """
        Se llama DESPUÉS de cerrar T0.

        Sólo registra un exhaustion potencial.
        Nunca abre una posición aquí.
        """

        symbol = str(
            event["symbol"]
        ).upper()

        # Sólo LONG V1.
        move_3m = event.get(
            "move_3m_pct"
        )

        rsi = event.get(
            "rsi_1m"
        )

        rel_vol = event.get(
            "relative_volume"
        )

        vol3m = event.get(
            "volume_3m_ratio"
        )

        hv5 = event.get(
            "high_volume_candles_5m"
        )

        if any(
            value is None
            for value in (
                move_3m,
                rsi,
                rel_vol,
                vol3m,
                hv5,
            )
        ):
            return False

        if float(rsi) > self.rsi_max:
            return False

        if (
            float(rel_vol)
            < self.relative_volume_min
        ):
            return False

        if (
            float(vol3m)
            < self.volume_3m_min
        ):
            return False

        if int(hv5) < self.hv5_min:
            return False

        # Queremos caída fuerte:
        # -1.0 <= -0.75 -> válido.
        if (
            float(move_3m)
            > self.move_3m_max
        ):
            return False

        (
            running_low,
            distance_low_pct,
        ) = self._running_low_context(
            event
        )

        if (
            running_low is None
            or distance_low_pct is None
        ):
            return False

        if (
            distance_low_pct
            > self.max_distance_low_pct
        ):
            return False

        # No superponer setups del mismo símbolo.
        if (
            symbol in self.waiting
            or symbol in self.pending_entries
            or symbol in self.positions
        ):
            return False

        state = {
            "event": dict(event),
            "event_close_time": int(
                event[
                    "candle_close_timestamp"
                ]
            ),
            "event_close": float(
                event["close"]
            ),
            "running_low_30m": float(
                running_low
            ),
            "distance_low_30m_pct": float(
                distance_low_pct
            ),
        }

        self.waiting[
            symbol
        ] = state

        self.detected += 1

        print(
            "[VE DETECTED] "
            f"symbol={symbol} "
            f"ts={state['event_close_time']} "
            f"close={state['event_close']:.8f} "
            f"rsi={float(rsi):.2f} "
            f"vol={float(rel_vol):.2f}x "
            f"vol3m={float(vol3m):.2f}x "
            f"hv5={int(hv5)} "
            f"move3m={float(move_3m):.3f}% "
            f"low30={running_low:.8f} "
            f"distLow={distance_low_pct:.3f}%"
        )

        return True

    # =====================================================
    # EVERY CLOSED 1m CANDLE
    # =====================================================

    def on_candle(
        self,
        symbol,
        close_time,
        candle,
    ):
        """
        Orden temporal:

        1. si había entry pendiente, entra al OPEN actual
        2. evalúa TP/SL con OHLC actual
        3. evalúa confirmación de eventos anteriores

        El evento NUEVO T0 se pasa después por on_event().
        """

        symbol = str(
            symbol
        ).upper()

        close_time = int(
            close_time
        )

        last = self.last_processed_close.get(
            symbol
        )

        if (
            last is not None
            and close_time <= last
        ):
            return

        self.last_processed_close[
            symbol
        ] = close_time

        # =============================================
        # ENTRY PROGRAMADA DESDE VELA ANTERIOR
        # =============================================

        pending = self.pending_entries.get(
            symbol
        )

        if pending is not None:

            confirmation_close = int(
                pending[
                    "confirmation_close_time"
                ]
            )

            if close_time > confirmation_close:

                candle_open_ts = int(
                    candle["timestamp"]
                )

                expected_open_ts = (
                    confirmation_close + 1
                )

                if (
                    candle_open_ts
                    != expected_open_ts
                ):
                    print(
                        "[VE ENTRY CANCELLED GAP] "
                        f"symbol={symbol} "
                        f"expected_open="
                        f"{expected_open_ts} "
                        f"actual_open="
                        f"{candle_open_ts}"
                    )

                    self.pending_entries.pop(
                        symbol,
                        None,
                    )

                else:
                    self._open_position(
                        symbol=symbol,
                        candle=candle,
                        close_time=close_time,
                        pending=pending,
                    )

                    self.pending_entries.pop(
                        symbol,
                        None,
                    )

        # =============================================
        # POSITION
        # =============================================

        if symbol in self.positions:

            self._process_position(
                symbol=symbol,
                close_time=close_time,
                candle=candle,
            )

        # =============================================
        # CONFIRMATION
        # =============================================

        waiting = self.waiting.get(
            symbol
        )

        if waiting is None:
            return

        event_close_time = int(
            waiting["event_close_time"]
        )

        if close_time <= event_close_time:
            return

        age_bars = int(
            (
                close_time
                - event_close_time
            )
            // MINUTE_MS
        )

        current_close = float(
            candle["close"]
        )

        # Confirmación simple:
        # cierre posterior por encima de event_close.
        if (
            1 <= age_bars
            <= self.confirmation_bars
            and current_close
            > waiting["event_close"]
        ):
            self.confirmed += 1

            self.pending_entries[
                symbol
            ] = {
                **waiting,
                "confirmation_close_time": (
                    close_time
                ),
                "confirmation_close": (
                    current_close
                ),
                "confirmation_delay_bars": (
                    age_bars
                ),
            }

            self.waiting.pop(
                symbol,
                None,
            )

            print(
                "[VE CONFIRMED] "
                f"symbol={symbol} "
                f"event_ts={event_close_time} "
                f"confirm_ts={close_time} "
                f"delay={age_bars} "
                f"event_close="
                f"{waiting['event_close']:.8f} "
                f"confirm_close="
                f"{current_close:.8f}"
            )

            return

        # Se evalúa también la tercera vela.
        # Si no confirmó, recién ahí expira.
        if age_bars >= self.confirmation_bars:

            self.expired += 1

            self.waiting.pop(
                symbol,
                None,
            )

            print(
                "[VE EXPIRED] "
                f"symbol={symbol} "
                f"event_ts={event_close_time} "
                f"age={age_bars}"
            )

    # =====================================================
    # OPEN
    # =====================================================

    def _open_position(
        self,
        symbol,
        candle,
        close_time,
        pending,
    ):
        entry_price = float(
            candle["open"]
        )

        entry_ts = int(
            candle["timestamp"]
        )

        quantity = (
            self.notional_usdt
            / entry_price
        )

        tp = entry_price * (
            1.0
            + self.tp_pct / 100.0
        )

        sl = entry_price * (
            1.0
            - self.sl_pct / 100.0
        )

        entry_fee = (
            self.notional_usdt
            * self.taker_fee_pct
            / 100.0
        )

        self.positions[
            symbol
        ] = {
            **pending,
            "symbol": symbol,
            "side": "LONG",
            "entry_price": entry_price,
            "entry_ts": entry_ts,
            "entry_close_time": close_time,
            "quantity": quantity,
            "tp": tp,
            "sl": sl,
            "entry_fee": entry_fee,
            "bars_held": 0,
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
        }

        print(
            "[VE REPLAY ENTRY] "
            f"symbol={symbol} "
            f"side=LONG "
            f"entry={entry_price:.8f} "
            f"tp={tp:.8f} "
            f"sl={sl:.8f} "
            f"ts={entry_ts}"
        )

    # =====================================================
    # POSITION MANAGEMENT
    # =====================================================

    def _process_position(
        self,
        symbol,
        close_time,
        candle,
    ):
        pos = self.positions[
            symbol
        ]

        entry = float(
            pos["entry_price"]
        )

        high = float(
            candle["high"]
        )

        low = float(
            candle["low"]
        )

        close = float(
            candle["close"]
        )

        mfe_pct = (
            (high / entry) - 1.0
        ) * 100.0

        mae_pct = (
            (low / entry) - 1.0
        ) * 100.0

        pos["mfe_pct"] = max(
            float(pos["mfe_pct"]),
            mfe_pct,
        )

        pos["mae_pct"] = min(
            float(pos["mae_pct"]),
            mae_pct,
        )

        sl_hit = (
            low <= float(pos["sl"])
        )

        tp_hit = (
            high >= float(pos["tp"])
        )

        # Política conservadora:
        # si toca ambos en misma vela, gana SL.
        if sl_hit:

            self._close_position(
                symbol=symbol,
                close_time=close_time,
                exit_price=float(
                    pos["sl"]
                ),
                reason="SL",
                ambiguous=tp_hit,
            )

            return

        if tp_hit:

            self._close_position(
                symbol=symbol,
                close_time=close_time,
                exit_price=float(
                    pos["tp"]
                ),
                reason="TP",
                ambiguous=False,
            )

            return

        pos["bars_held"] += 1

        if (
            pos["bars_held"]
            >= self.max_hold_bars
        ):
            self._close_position(
                symbol=symbol,
                close_time=close_time,
                exit_price=close,
                reason="TIME_EXIT",
                ambiguous=False,
            )

    # =====================================================
    # CLOSE
    # =====================================================

    def _close_position(
        self,
        symbol,
        close_time,
        exit_price,
        reason,
        ambiguous,
    ):
        pos = self.positions.pop(
            symbol
        )

        entry = float(
            pos["entry_price"]
        )

        quantity = float(
            pos["quantity"]
        )

        exit_price = float(
            exit_price
        )

        gross_pnl = (
            exit_price - entry
        ) * quantity

        entry_fee = float(
            pos["entry_fee"]
        )

        exit_notional = (
            quantity * exit_price
        )

        exit_fee = (
            exit_notional
            * self.taker_fee_pct
            / 100.0
        )

        total_fees = (
            entry_fee + exit_fee
        )

        net_pnl = (
            gross_pnl
            - total_fees
        )

        row = {
            **pos["event"],

            "running_low_30m": (
                pos["running_low_30m"]
            ),
            "distance_low_30m_pct": (
                pos[
                    "distance_low_30m_pct"
                ]
            ),

            "confirmation_close_time": (
                pos[
                    "confirmation_close_time"
                ]
            ),
            "confirmation_delay_bars": (
                pos[
                    "confirmation_delay_bars"
                ]
            ),

            "entry_ts": (
                pos["entry_ts"]
            ),
            "entry_price": entry,

            "tp_price": pos["tp"],
            "sl_price": pos["sl"],

            "exit_ts": int(
                close_time
            ),
            "exit_price": exit_price,
            "exit_reason": reason,

            "bars_held": (
                pos["bars_held"]
            ),

            "mfe_pct": (
                pos["mfe_pct"]
            ),
            "mae_pct": (
                pos["mae_pct"]
            ),

            "gross_pnl": gross_pnl,
            "entry_fee": entry_fee,
            "exit_fee": exit_fee,
            "total_fees": total_fees,
            "net_pnl": net_pnl,

            "net_return_pct": (
                net_pnl
                / self.notional_usdt
                * 100.0
            ),

            "ambiguous": bool(
                ambiguous
            ),
        }

        self.trades.append(
            row
        )

        self._save_trade(
            row
        )

        print(
            "[VE REPLAY EXIT] "
            f"symbol={symbol} "
            f"reason={reason} "
            f"entry={entry:.8f} "
            f"exit={exit_price:.8f} "
            f"net={net_pnl:.5f} "
            f"mfe={pos['mfe_pct']:.3f}% "
            f"mae={pos['mae_pct']:.3f}% "
            f"ambiguous={ambiguous}"
        )

        self._print_summary()

    # =====================================================
    # CSV
    # =====================================================

    def _save_trade(
        self,
        row,
    ):
        exists = (
            self.trades_path.exists()
        )

        with self.trades_path.open(
            "a",
            newline="",
            encoding="utf-8",
        ) as file:

            writer = csv.DictWriter(
                file,
                fieldnames=list(
                    row.keys()
                ),
            )

            if not exists:
                writer.writeheader()

            writer.writerow(
                row
            )

    # =====================================================
    # SUMMARY
    # =====================================================

    def _print_summary(
        self,
    ):
        if not self.trades:
            return

        net_winners = [
            trade
            for trade in self.trades
            if float(
                trade["net_pnl"]
            ) > 0
        ]

        net_losers = [
            trade
            for trade in self.trades
            if float(
                trade["net_pnl"]
            ) < 0
        ]

        gross_profit = sum(
            float(
                trade["net_pnl"]
            )
            for trade in net_winners
        )

        gross_loss = abs(
            sum(
                float(
                    trade["net_pnl"]
                )
                for trade in net_losers
            )
        )

        if gross_loss > 0:
            profit_factor = (
                gross_profit
                / gross_loss
            )
        else:
            profit_factor = float(
                "inf"
            )

        trades = len(
            self.trades
        )

        win_rate = (
            len(net_winners)
            / trades
            * 100.0
        )

        tp_count = sum(
            trade["exit_reason"] == "TP"
            for trade in self.trades
        )

        sl_count = sum(
            trade["exit_reason"] == "SL"
            for trade in self.trades
        )

        time_count = sum(
            trade["exit_reason"]
            == "TIME_EXIT"
            for trade in self.trades
        )

        total_net = sum(
            float(
                trade["net_pnl"]
            )
            for trade in self.trades
        )

        print(
            "[VE SUMMARY] "
            f"trades={trades} "
            f"TP={tp_count} "
            f"SL={sl_count} "
            f"TIME={time_count} "
            f"net_wins={len(net_winners)} "
            f"WR={win_rate:.2f}% "
            f"PF_NET={profit_factor:.3f} "
            f"NET={total_net:.4f}"
        )