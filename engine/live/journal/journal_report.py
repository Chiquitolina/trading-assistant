import pandas as pd
from pathlib import Path

from engine.backtest.metrics import calculate_metrics, pretty_metrics
from ui.trade_formatter import format_trade_journal
from ui.banners import print_journal_banner

# 📍 subir hasta la raíz del proyecto
BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
TRADES_FILE = BASE_DIR / "trades.csv"


# =====================================================
# TRADE CARDS
# =====================================================
def print_trade_cards(df):
    for _, row in df.iterrows():
        print("╔══════════════════════════════════════════════════╗")
        print(f"║ 🕒 Signal : {row['signal_ts']:<34}   ║")
        print(f"║ Signal Price : {row['signal_price']:<34}║")
        print(f"║ 📈 Side   : {row['side']:<34}   ║")
        print("╠══════════════════════════════════════════════════╣")
        print(f"║ Entry    : {row['entry']:<34}    ║")
        print(f"║ Exit     : {row['exit']:<34}    ║")
        print(f"║ TP / SL  : {row['tp']} / {row['sl']:<25} ║")
        print("╠══════════════════════════════════════════════════╣")
        print(f"║ PnL      : {row['pnl']:<37} ║")
        print(f"║ Fees     : {row['fees']:<37} ║")
        print(f"║ Result   : {row['reason']:<37} ║")
        print("╚══════════════════════════════════════════════════╝\n")


# =====================================================
# MAIN JOURNAL VIEW
# =====================================================
def show_trade_journal():
    if not TRADES_FILE.exists():
        print(f"❌ No trades.csv found at {TRADES_FILE}")
        return

    raw_df = pd.read_csv(TRADES_FILE)

    if raw_df.empty:
        print("📭 Trade journal vacío")
        return

    # UI formatted copy
    df = format_trade_journal(raw_df.copy())

    if "entry_ts" in df.columns:
        df = df.sort_values("entry_ts")

    print_journal_banner()

    # ================= METRICS =================
    all_trades = raw_df.to_dict(orient="records")
    long_trades = raw_df[raw_df["side"] == "LONG"].to_dict(orient="records")
    short_trades = raw_df[raw_df["side"] == "SHORT"].to_dict(orient="records")

    metrics_all = calculate_metrics(all_trades)
    metrics_long = calculate_metrics(long_trades)
    metrics_short = calculate_metrics(short_trades)

    print(pretty_metrics(metrics_all, metrics_long, metrics_short))

    # ================= CARDS =================
    print("\n📌 TRADES DETAILS:\n")
    print_trade_cards(df)


if __name__ == "__main__":
    show_trade_journal()