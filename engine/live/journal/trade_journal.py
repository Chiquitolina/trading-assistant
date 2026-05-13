# engine/live/journal/trade_journal.py

import csv
import os


class TradeJournal:

    def __init__(self, file_path="trades.csv"):
        self.file_path = file_path
        self._ensure_file()

    # -------------------------
    # crear archivo si no existe
    # -------------------------
    def _ensure_file(self):
        if not os.path.exists(self.file_path):
            with open(self.file_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)

                writer.writerow([
                    "signal_ts",
                    "signal_price",

                    "entry_ts",
                    "exit_ts",
                    "side",

                    "entry",
                    "real_entry",

                    "exit",
                    "real_exit",

                    "tp",
                    "sl",

                    "pnl",
                    "pnl_gross",
                    "pnl_usd",
                    "fees",

                    "exit_reason",

                    "signal_trend",
                    "signal_direction",
                    "signal_momentum",

                    # 🔥 NUEVO CONTEXTO
                    "signal_momentum_prev1",
                    "signal_momentum_prev2",
                    "signal_momentum_sequence",

                    "signal_atr",

                    # =========================
                    # POST ENTRY ANALYSIS
                    # =========================
                    "current_trend",
                    "current_direction",
                    "current_momentum",

                    "direction_t1",
                    "momentum_t1",

                    "pnl_t1",

                    "mae",
                    "mfe",
                    
                    "strategy_mode",
                    "router_reason"
                ])

    # -------------------------
    # guardar trade
    # -------------------------
    def log_trade(
        self,
        signal_ts,
        signal_price,
        entry_ts,
        exit_ts,
        side,
        entry,
        real_entry,
        exit_price,
        real_exit,
        tp,
        sl,
        pnl,
        pnl_usd,
        pnl_gross,
        fees,
        exit_reason,
        
        signal_trend,
        signal_direction,
        signal_momentum,

        # 🔥 NUEVO CONTEXTO
        signal_momentum_prev1=None,
        signal_momentum_prev2=None,
        signal_momentum_sequence=None,

        signal_atr=None,

        # =========================
        # POST ENTRY ANALYSIS
        # =========================
        current_trend=None,
        current_direction=None,
        current_momentum=None,

        direction_t1=None,
        momentum_t1=None,

        pnl_t1=None,

        mae=None,
        mfe=None,
        
        strategy_mode=None,
        router_reason=None
    ):

        with open(self.file_path, "a", newline="", encoding="utf-8") as f:

            writer = csv.writer(f)

            writer.writerow([
                signal_ts,
                round(signal_price, 2),

                entry_ts,
                exit_ts,
                side,

                round(entry, 2),
                round(real_entry, 2),

                round(exit_price, 2),
                round(real_exit, 2),

                round(tp, 2),
                round(sl, 2),

                round(pnl, 4),
                round(pnl_gross, 4),
                round(pnl_usd, 2),
                round(fees, 2),

                exit_reason,

                signal_trend,
                signal_direction,
                signal_momentum,

                # 🔥 NUEVO CONTEXTO
                signal_momentum_prev1,
                signal_momentum_prev2,
                signal_momentum_sequence,

                signal_atr,

                # =========================
                # POST ENTRY ANALYSIS
                # =========================
                current_trend,
                current_direction,
                current_momentum,

                direction_t1,
                momentum_t1,

                round(pnl_t1, 4) if pnl_t1 is not None else None,

                round(mae, 4) if mae is not None else None,
                round(mfe, 4) if mfe is not None else None,
                
                strategy_mode,
                router_reason
            ])