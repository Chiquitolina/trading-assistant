def safe_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None
    
def get_quote_volume_column(df):
    if df is None or df.empty:
        return None

    for col in [
        "quoteVolume",
        "quote_volume",
        "quote_asset_volume",
        "q",
    ]:
        if col in df.columns:
            return col

    return None


def get_avg_quote_volume(df, lookback: int):
    col = get_quote_volume_column(df)

    if col is None:
        return None

    data = df.tail(lookback)

    if data.empty:
        return None

    return safe_float(data[col].mean())


def get_relative_volume(df, lookback: int):
    col = get_quote_volume_column(df)

    if col is None:
        return None

    if len(df) < lookback + 1:
        return None

    current_volume = safe_float(df.iloc[-1][col])
    avg_volume = safe_float(df.iloc[-lookback-1:-1][col].mean())

    if current_volume is None or avg_volume is None or avg_volume <= 0:
        return None

    return current_volume / avg_volume


def volume_tier(quote_volume_24h):
    qv = safe_float(quote_volume_24h)

    if qv is None:
        return "unknown"

    if qv >= 1_000_000_000:
        return "very_high"

    if qv >= 250_000_000:
        return "high"

    if qv >= 50_000_000:
        return "medium"

    if qv >= 10_000_000:
        return "low"

    return "very_low"


def rvol_tier(rvol):
    rv = safe_float(rvol)

    if rv is None:
        return "unknown"

    if rv >= 3:
        return "extreme"

    if rv >= 2:
        return "high"

    if rv >= 1.2:
        return "above_average"

    if rv >= 0.8:
        return "normal"

    return "low"


def build_liquidity_context(
    df_15m=None,
    df_1h=None,
    df_4h=None,
    quote_volume_24h=None,
    lookback=20,
):
    avg_quote_volume_15m = get_avg_quote_volume(df_15m, lookback)
    avg_quote_volume_1h = get_avg_quote_volume(df_1h, lookback)
    avg_quote_volume_4h = get_avg_quote_volume(df_4h, lookback)

    relative_volume_15m = get_relative_volume(df_15m, lookback)
    relative_volume_1h = get_relative_volume(df_1h, lookback)
    relative_volume_4h = get_relative_volume(df_4h, lookback)

    return {
        "quote_volume_24h": safe_float(quote_volume_24h),

        "avg_quote_volume_15m": avg_quote_volume_15m,
        "avg_quote_volume_1h": avg_quote_volume_1h,
        "avg_quote_volume_4h": avg_quote_volume_4h,

        "relative_volume_15m": relative_volume_15m,
        "relative_volume_1h": relative_volume_1h,
        "relative_volume_4h": relative_volume_4h,

        "volume_tier": volume_tier(quote_volume_24h),
        "rvol_tier_15m": rvol_tier(relative_volume_15m),
        "rvol_tier_1h": rvol_tier(relative_volume_1h),
        "rvol_tier_4h": rvol_tier(relative_volume_4h),
    }