import pandas as pd

TZ = "America/Argentina/Buenos_Aires"

def format_trade_timestamps(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()

    df["entry_ts"] = (
        pd.to_datetime(df["entry_ts"], unit="ms", utc=True)
        .dt.tz_convert("America/Argentina/Buenos_Aires")
        .dt.strftime("%m-%d %H:%M")
    )

    df["exit_ts"] = (
        pd.to_datetime(df["exit_ts"], unit="ms", utc=True)
        .dt.tz_convert("America/Argentina/Buenos_Aires")
        .dt.strftime("%m-%d %H:%M")
    )

    return df
