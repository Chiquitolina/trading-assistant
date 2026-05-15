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
                    "symbol",
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
                    # HTF EXTENSION CONTEXT
                    # =========================
                    "dist_ema50_15m_pct",
                    "dist_ema99_15m_pct",

                    "dist_ema50_1h_pct",
                    "dist_ema99_1h_pct",

                    "dist_ema50_4h_pct",
                    "dist_ema99_4h_pct",

                    # =========================
                    # POST ENTRY ANALYSIS
                    # =========================
                    "current_trend",
                    "current_direction",
                    "current_momentum",

                    "direction_t1",
                    "momentum_t1",

                    "pnl_t1",

                    "micro_t1",
                    "direction_5m_t1",

                    "reclaimed_ema20_1m",
                    "reclaimed_ema34_1m",
                    "reclaimed_ema50_1m",

                    "lost_ema20_1m",
                    "lost_ema34_1m",
                    "lost_ema50_1m",

                    "dist_ema20_1m_pct",
                    "dist_ema34_1m_pct",
                    "dist_ema50_1m_pct",

                    "max_favorable_pct",
                    "max_adverse_pct",

                    "direction_5m_changed",
                    "direction_5m_after_entry",

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
        symbol,
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
        # HTF EXTENSION CONTEXT
        # =========================
        dist_ema50_15m_pct=None,
        dist_ema99_15m_pct=None,

        dist_ema50_1h_pct=None,
        dist_ema99_1h_pct=None,

        dist_ema50_4h_pct=None,
        dist_ema99_4h_pct=None,

        # =========================
        # POST ENTRY ANALYSIS
        # =========================
        current_trend=None,
        current_direction=None,
        current_momentum=None,

        direction_t1=None,
        momentum_t1=None,

        pnl_t1=None,

        micro_t1=None,
        direction_5m_t1=None,

        reclaimed_ema20_1m=False,
        reclaimed_ema34_1m=False,
        reclaimed_ema50_1m=False,

        lost_ema20_1m=False,
        lost_ema34_1m=False,
        lost_ema50_1m=False,

        dist_ema20_1m_pct=None,
        dist_ema34_1m_pct=None,
        dist_ema50_1m_pct=None,

        max_favorable_pct=None,
        max_adverse_pct=None,

        direction_5m_changed=False,
        direction_5m_after_entry=None,

        mae=None,
        mfe=None,

        strategy_mode=None,
        router_reason=None
    ):

        with open(self.file_path, "a", newline="", encoding="utf-8") as f:

            writer = csv.writer(f)

            writer.writerow([
                symbol,
                signal_ts,
                round(signal_price, 2),

                entry_ts,
                exit_ts,
                side,

                round(entry, 8),
                round(real_entry, 8),

                round(exit_price, 8),
                round(real_exit, 8),

                round(tp, 4),
                round(sl, 4),

                round(pnl, 4),
                round(pnl_gross, 4),
                round(pnl_usd, 4),
                round(fees, 4),

                exit_reason,

                signal_trend,
                signal_direction,
                signal_momentum,

                # 🔥 NUEVO CONTEXTO
                signal_momentum_prev1,
                signal_momentum_prev2,
                signal_momentum_sequence,

                signal_atr,
                
                round(dist_ema50_15m_pct, 4) if dist_ema50_15m_pct is not None else None,
                round(dist_ema99_15m_pct, 4) if dist_ema99_15m_pct is not None else None,

                round(dist_ema50_1h_pct, 4) if dist_ema50_1h_pct is not None else None,
                round(dist_ema99_1h_pct, 4) if dist_ema99_1h_pct is not None else None,

                round(dist_ema50_4h_pct, 4) if dist_ema50_4h_pct is not None else None,
                round(dist_ema99_4h_pct, 4) if dist_ema99_4h_pct is not None else None,

                # =========================
                # POST ENTRY ANALYSIS
                # =========================
                current_trend,
                current_direction,
                current_momentum,

                direction_t1,
                momentum_t1,

                round(pnl_t1, 4) if pnl_t1 is not None else None,

                micro_t1,
                direction_5m_t1,

                reclaimed_ema20_1m,
                reclaimed_ema34_1m,
                reclaimed_ema50_1m,

                lost_ema20_1m,
                lost_ema34_1m,
                lost_ema50_1m,

                round(dist_ema20_1m_pct, 4) if dist_ema20_1m_pct is not None else None,
                round(dist_ema34_1m_pct, 4) if dist_ema34_1m_pct is not None else None,
                round(dist_ema50_1m_pct, 4) if dist_ema50_1m_pct is not None else None,

                round(max_favorable_pct, 4) if max_favorable_pct is not None else None,
                round(max_adverse_pct, 4) if max_adverse_pct is not None else None,

                direction_5m_changed,
                direction_5m_after_entry,

                round(mae, 4) if mae is not None else None,
                round(mfe, 4) if mfe is not None else None,

                strategy_mode,
                router_reason
            ])