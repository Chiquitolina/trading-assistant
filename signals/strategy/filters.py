def min_expected_tp_ok(entry_price, atr, tp_mult, min_tp_pct):
    expected_tp_pct = (atr * tp_mult) / entry_price * 100
    return expected_tp_pct >= min_tp_pct