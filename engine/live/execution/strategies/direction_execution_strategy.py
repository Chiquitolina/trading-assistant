from engine.live.execution.strategies.base_execution_strategy import BaseExecutionStrategy


class DirectionExecutionStrategy(BaseExecutionStrategy):
    
    BE_TRIGGER = None

    # ==========================================================
    # SIGNAL → FLIP LOGIC
    # ==========================================================
    def on_signal(self, execution_engine, trade_action, plan):

        pos = execution_engine.get_position(plan.symbol)

        if not pos:
            return execution_engine.open_position(plan)

        if pos.side == plan.side:
            print("[DIRECTION] same side -> HOLD")
            return False

        print("[DIRECTION] opposite signal ignored")
        return False

    # ==========================================================
    # PRICE UPDATE → BE + TRAILING
    # ==========================================================
    def on_price_update(self, execution_engine, symbol, price, timestamp):

        pos = execution_engine.get_position(symbol)
        if not pos:
            return

        pnl = (
            (price - pos.real_entry) / pos.real_entry * 100
            if pos.side == "LONG"
            else (pos.real_entry - price) / pos.real_entry * 100
        )

        pos.current_pnl = pnl

        pos.mae = pnl if pos.mae is None else min(pos.mae, pnl)
        pos.mfe = pnl if pos.mfe is None else max(pos.mfe, pnl)

        if self.BE_TRIGGER is not None and pnl >= self.BE_TRIGGER:
            
            if pos.be_moved:
                return

            if pos.side == "LONG" and pos.sl < pos.real_entry:
                if execution_engine.move_sl_to_be(pos):
                    print("[DIRECTION][BE] LONG -> real entry")

            elif pos.side == "SHORT" and pos.sl > pos.real_entry:
                if execution_engine.move_sl_to_be(pos):
                    print("[DIRECTION][BE] SHORT -> real entry")
                
    # ==========================================================
    # CANDLE CLOSE → TIME STOP
    # ==========================================================
    def on_candle_close(self, execution_engine, closed_candle_ts, close_price):
        return
    
    # ==========================================================
    # CONTEXT
    # ==========================================================
    # ==========================================================
    # CONTEXT UPDATE (ENGINE VIEJO EXACTO)
    # ==========================================================
    def update_position_context(
        self,
        execution_engine,
        symbol,
        trend,
        direction,
        momentum,
        micro_momentum=None,
        current_price=None,
        ema20_1m=None,
        ema34_1m=None,
        ema50_1m=None
    ):

        pos = execution_engine.get_position(symbol)
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
        
        #print(
        #    f"[POST ENTRY] "
        #    f"{symbol} | "
        #    f"pnl={pos.current_pnl} | "
        #    f"mfe={pos.mfe} | "
        #    f"mae={pos.mae} | "
        #    f"dir={pos.current_direction} | "
        #    f"mom={pos.current_momentum}"
        #)