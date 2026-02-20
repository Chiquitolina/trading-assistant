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

#----------------------------------------
#JOURNAL FORMATTERS
#----------------------------------------

def format_journal_dates(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    for col in ["entry_ts", "exit_ts"]:
        if col in df.columns:
            df[col] = (
                pd.to_datetime(df[col], errors="coerce", utc=True)
                .dt.tz_convert(TZ)
                .dt.strftime("%d-%m %H:%M")
            )

    return df


def format_journal_numbers(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    price_cols = ["entry", "exit", "tp", "sl"]
    for col in price_cols:
        if col in df.columns:
            df[col] = df[col].map(lambda x: f"{x:,.2f}")

    if "pnl_pct" in df.columns:
        df["pnl_pct"] = df["pnl_pct"].map(lambda x: f"{x*100:.2f}%")

    return df


def format_trade_journal(df: pd.DataFrame) -> pd.DataFrame:
    df = format_journal_dates(df)
    df = format_journal_numbers(df)
    return df
