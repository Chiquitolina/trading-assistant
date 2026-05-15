from engine.live.execution.strategies.base_execution_strategy import BaseExecutionStrategy
from enums.actions import Action


class DefaultExecutionStrategy(BaseExecutionStrategy):

    # ==========================================================
    # SIGNAL HANDLING (ENGINE VIEJO EXACTO)
    # ==========================================================
    def on_signal(self, execution_engine, trade_action, plan):

        if trade_action.action == Action.HOLD:
            return False

        pos = execution_engine.get_position(plan.symbol)

        # NO POSITION → OPEN
        if not pos:
            print("[DEFAULT] open position")
            return execution_engine.open_position(plan)

        # SAME SIDE → IGNORE
        if pos.side == plan.side:
            print("[DEFAULT] same side -> HOLD")
            return False

        # ❌ NO FLIP
        print("[DEFAULT] opposite signal ignored")
        return False

    # ==========================================================
    # PRICE UPDATE (ENGINE VIEJO COMPLETO)
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

        # ==========================
        # MAE / MFE SAFE UPDATE
        # ==========================
        pos.mae = pnl if pos.mae is None else min(pos.mae, pnl)
        pos.mfe = pnl if pos.mfe is None else max(pos.mfe, pnl)

        # ==========================
        # BREAK EVEN
        # ==========================
        BE_TRIGGER = 0.20

        if pnl >= BE_TRIGGER:

            if pos.side == "LONG" and pos.sl < pos.real_entry:
                pos.sl = pos.real_entry
                print("[DEFAULT][BE] LONG -> entry")

            elif pos.side == "SHORT" and pos.sl > pos.real_entry:
                pos.sl = pos.real_entry
                print("[DEFAULT][BE] SHORT -> entry")

        # ==========================
        # TRAILING STOP
        # ==========================
        TRAIL_TRIGGER = 0.40

        if pnl >= TRAIL_TRIGGER:

            offset = 0.15

            if pos.side == "LONG":
                new_sl = pos.real_entry * (1 + (pnl - offset) / 100)
                if new_sl > pos.sl:
                    pos.sl = new_sl
                    print(f"[DEFAULT][TRAIL] LONG SL -> {new_sl:.4f}")

            else:
                new_sl = pos.real_entry * (1 - (pnl - offset) / 100)
                if new_sl < pos.sl:
                    pos.sl = new_sl
                    print(f"[DEFAULT][TRAIL] SHORT SL -> {new_sl:.4f}")

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

        if candles >= 24:

            print("[DEFAULT][TIME STOP] closing position")

            execution_engine._close_position(
                price=close_price,
                timestamp=closed_candle_ts,
                reason="TIME_STOP"
            )

    # ==========================================================
    # CONTEXT UPDATE (ENGINE VIEJO EXACTO)
    # ==========================================================
    def update_position_context(
        self,
        execution_engine,
        trend,
        direction,
        momentum,
        micro_momentum=None,
        current_price=None,
        ema20_1m=None,
        ema34_1m=None,
        ema50_1m=None
    ):

        pos = execution_engine.position
        if not pos:
            return

        # ==========================
        # CURRENT CONTEXT
        # ==========================
        pos.current_trend = trend
        pos.current_direction = direction
        pos.current_momentum = momentum

        # ==========================
        # FIRST POST-ENTRY STATE
        # ==========================
        if pos.direction_t1 is None:
            pos.direction_t1 = direction

        if pos.momentum_t1 is None:
            pos.momentum_t1 = momentum

        if pos.micro_t1 is None:
            pos.micro_t1 = micro_momentum

        if pos.direction_5m_t1 is None:
            pos.direction_5m_t1 = direction

        if current_price is None:
            return

        # ==========================
        # PNL
        # ==========================
        pnl = (
            (current_price - pos.real_entry) / pos.real_entry * 100
            if pos.side == "LONG"
            else (pos.real_entry - current_price) / pos.real_entry * 100
        )

        pos.current_pnl = pnl

        # ==========================
        # MAE / MFE
        # ==========================
        pos.mae = pnl if pos.mae is None else min(pos.mae, pnl)
        pos.mfe = pnl if pos.mfe is None else max(pos.mfe, pnl)

        if pos.pnl_t1 is None:
            pos.pnl_t1 = pnl

        # ==========================
        # EMA DISTANCES
        # ==========================
        if ema20_1m is not None and ema20_1m > 0:
            pos.dist_ema20_1m_pct = round(
                ((current_price - ema20_1m) / ema20_1m) * 100,
                4
            )

        if ema34_1m is not None and ema34_1m > 0:
            pos.dist_ema34_1m_pct = round(
                ((current_price - ema34_1m) / ema34_1m) * 100,
                4
            )

        if ema50_1m is not None and ema50_1m > 0:
            pos.dist_ema50_1m_pct = round(
                ((current_price - ema50_1m) / ema50_1m) * 100,
                4
            )

        # ==========================
        # TRADE EXCURSIONS
        # ==========================
        pos.max_favorable_pct = pos.mfe
        pos.max_adverse_pct = pos.mae