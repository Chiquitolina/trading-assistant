def compute_levels(side, entry, atr, cfg):
    if side == "LONG":
        sl = entry - cfg['sl_mult'] * atr
        tp = entry + cfg['tp_mult'] * atr
    else:
        sl = entry + cfg['sl_mult'] * atr
        tp = entry - cfg['tp_mult'] * atr

    sl_pct = abs((entry - sl) / entry * 100)
    tp_pct = abs((entry - tp) / entry * 100)

    return sl, tp, sl_pct, tp_pct

def compute_pnl(side, entry, exit_price, fees_cfg):
    if side == "LONG":
        pnl_gross = (exit_price - entry) / entry * 100
    else:  # SHORT
        pnl_gross = (entry - exit_price) / entry * 100

    fees = 2 * fees_cfg["taker"] + fees_cfg["funding"]
    pnl_net = pnl_gross - fees

    return (
        round(pnl_net, 3),
        round(pnl_gross, 3),
        round(fees, 3),
    )