import pandas as pd
from pathlib import Path

from engine.backtest.metrics import calculate_metrics
from ui.trade_formatter import format_trade_journal
from ui.banners import print_journal_banner

from engine.backtest.metrics import pretty_metrics

# 📍 subir hasta la raíz del proyecto
BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
TRADES_FILE = BASE_DIR / "trades.csv"


def show_trade_journal():
    if not TRADES_FILE.exists():
        print(f"❌ No trades.csv found at {TRADES_FILE}")
        return

    # =====================================================
    # RAW DATA (NUMÉRICO – PARA MÉTRICAS)
    # =====================================================
    raw_df = pd.read_csv(TRADES_FILE)

    if raw_df.empty:
        print("📭 Trade journal vacío")
        return

    # =====================================================
    # DISPLAY DATA (FORMATEADO – SOLO UI)
    # =====================================================
    df = format_trade_journal(raw_df.copy())

    if "entry_ts" in df.columns:
        df = df.sort_values("entry_ts")

    print_journal_banner()

    # =====================================================
    # MÉTRICAS → list[dict] (SIN FORMATO)
    # =====================================================
    all_trades = raw_df.to_dict(orient="records")
    long_trades = raw_df[raw_df["side"] == "LONG"].to_dict(orient="records")
    short_trades = raw_df[raw_df["side"] == "SHORT"].to_dict(orient="records")

    metrics_all = calculate_metrics(all_trades)
    metrics_long = calculate_metrics(long_trades)
    metrics_short = calculate_metrics(short_trades)

    # =====================================================
    # SUMMARY SIMPLE
    # =====================================================
    #total = len(all_trades)
    #wins = sum(1 for t in all_trades if t["pnl_pct"] > 0)
    #losses = total - wins
    #winrate = wins / total * 100 if total else 0
    #pnl_total = sum(t["pnl_pct"] for t in all_trades) * 100

    #    print("\n📊 SUMMARY")
    #    print(f"Trades     : {total}")
    #    print(f"Wins       : {wins}")
    #    print(f"Losses     : {losses}")
    #    print(f"Winrate    : {winrate:.2f}%")
    #    print(f"Total PnL  : {pnl_total:.2f}%\n")

    # =====================================================
    # TABLA DE MÉTRICAS
    # =====================================================
    print(
        pretty_metrics(
            metrics_all,
            metrics_long,
            metrics_short
        )
    )
    
    print('📌 TRADES DETAILS:\n')
    print(df.to_string(index=False, col_space=10))

if __name__ == "__main__":
    show_trade_journal()

