import pandas as pd
from pathlib import Path

from ui.trade_formatter import format_trade_journal

# 📍 subir hasta la raíz del proyecto
BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
TRADES_FILE = BASE_DIR / "trades.csv"


def show_trade_journal():
    if not TRADES_FILE.exists():
        print(f"❌ No trades.csv found at {TRADES_FILE}")
        return

    df = pd.read_csv(TRADES_FILE)

    if df.empty:
        print("📭 Trade journal vacío")
        return

    # -----------------------------
    # FORMATEO SOLO PARA JOURNAL
    # -----------------------------
    df = format_trade_journal(df)

    # -----------------------------
    # ORDENAR (si existe entry_time)
    # -----------------------------
    if "entry_time" in df.columns:
        df = df.sort_values("entry_time")

    print("\n📒 TRADE JOURNAL (Live)\n")
    print(df.to_string(index=False, col_space=12))

    # -----------------------------
    # MÉTRICAS RÁPIDAS
    # (usar df sin strings)
    # -----------------------------
    raw_df = pd.read_csv(TRADES_FILE)

    total = len(raw_df)
    wins = (raw_df["pnl_pct"] > 0).sum()
    losses = (raw_df["pnl_pct"] <= 0).sum()
    winrate = wins / total * 100 if total else 0
    pnl_total = raw_df["pnl_pct"].sum() * 100

    print("\n📊 SUMMARY")
    print(f"Trades     : {total}")
    print(f"Wins       : {wins}")
    print(f"Losses     : {losses}")
    print(f"Winrate    : {winrate:.2f}%")
    print(f"Total PnL  : {pnl_total:.2f}%\n")


if __name__ == "__main__":
    show_trade_journal()
