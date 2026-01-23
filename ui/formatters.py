from colorama import Fore, Style, init

init(autoreset=True)

# --------------------
# FORMATTERS
# --------------------
def color_trend(trend: str) -> str:
    if trend == "bullish":
        return f"{Fore.GREEN}BULLISH{Style.RESET_ALL}"
    if trend == "bearish":
        return f"{Fore.RED}BEARISH{Style.RESET_ALL}"
    return "neutral"


def color_direction(direction: str) -> str:
    if direction == "up":
        return f"{Fore.GREEN}UP ⬆{Style.RESET_ALL}"
    if direction == "down":
        return f"{Fore.RED}DOWN ⬇{Style.RESET_ALL}"
    return direction.upper()


def color_momentum(m: str) -> str:
    if m == "breakout_up":
        return f"{Fore.GREEN}BO ↑{Style.RESET_ALL}"
    if m == "breakout_down":
        return f"{Fore.RED}BO ↓{Style.RESET_ALL}"
    return "—"

