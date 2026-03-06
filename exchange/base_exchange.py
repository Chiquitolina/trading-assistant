from abc import ABC, abstractmethod


class BaseExchange(ABC):

    @abstractmethod
    def ping(self) -> bool:
        """Check basic connectivity with the exchange"""
        pass

    @abstractmethod
    def check_account(self) -> bool:
        """Check if API keys are valid and account is accessible"""
        pass

    @abstractmethod
    def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float
    ):
        """Place a market order"""
        pass

    @abstractmethod
    def close_position(
        self,
        symbol: str,
        side: str,
        quantity: float
    ):
        """Close an existing position"""
        pass