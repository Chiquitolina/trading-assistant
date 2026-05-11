from engine.live.execution.strategies.base_execution_strategy import BaseExecutionStrategy


class DirectionExecutionStrategy(BaseExecutionStrategy):

    # ==========================================================
    # SIGNAL → FLIP LOGIC
    # ==========================================================
    def on_signal(self, execution_engine, trade_action, plan):

        pos = execution_engine.position

        if not pos:
            return execution_engine.open_position(plan)

        if pos.side == plan.side:
            return False

        print(f"[DIRECTION] FLIP {pos.side} -> {plan.side}")

        execution_engine._close_position(
            price=plan.entry,
            timestamp=plan.signal_ts,
            reason="DIRECTION_FLIP"
        )

        return execution_engine.open_position(plan)

    # ==========================================================
    # PRICE UPDATE → BE + TRAILING
    # ==========================================================
    def on_price_update(self, execution_engine, price, timestamp):

        pos = execution_engine.position
        if not pos:
            return

        pnl = (
            (price - pos.real_entry) / pos.real_entry * 100
            if pos.side == "LONG"
            else (pos.real_entry - price) / pos.real_entry * 100
        )

        pos.current_pnl = pnl
        pos.mae = min(pos.mae, pnl)
        pos.mfe = max(pos.mfe, pnl)

        # ==========================
        # BREAK EVEN
        # ==========================
        if pnl >= 0.20:

            if pos.side == "LONG" and pos.sl < pos.real_entry:
                pos.sl = pos.real_entry

            if pos.side == "SHORT" and pos.sl > pos.real_entry:
                pos.sl = pos.real_entry

        # ==========================
        # TRAILING STOP
        # ==========================
        if pnl >= 0.40:

            buffer = 0.15

            if pos.side == "LONG":
                new_sl = price * (1 - buffer / 100)
                pos.sl = max(pos.sl, new_sl)

            else:
                new_sl = price * (1 + buffer / 100)
                pos.sl = min(pos.sl, new_sl)

    # ==========================================================
    # CANDLE CLOSE → TIME STOP
    # ==========================================================
    def on_candle_close(self, execution_engine, closed_candle_ts, close_price):

        pos = execution_engine.position
        if not pos:
            return

        candles = execution_engine._calc_candles_in_trade(
            closed_candle_ts,
            tf_minutes=15
        )

        pos.candles_in_trade = candles

        MAX_HOLD = pos.plan_max_hold_candles

        if candles >= MAX_HOLD:

            execution_engine._close_position(
                price=close_price,
                timestamp=closed_candle_ts,
                reason="TIME_STOP"
            )

    # ==========================================================
    # CONTEXT
    # ==========================================================
    def update_position_context(
        self,
        execution_engine,
        trend,
        direction,
        momentum,
        current_price
    ):

        pos = execution_engine.position
        if not pos:
            return

        pos.current_trend = trend
        pos.current_direction = direction
        pos.current_momentum = momentum
