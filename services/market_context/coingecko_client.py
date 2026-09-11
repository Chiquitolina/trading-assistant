import os
import threading
import time

import requests


class CoinGeckoClient:
    BASE_URL = (
        "https://api.coingecko.com/api/v3"
    )

    RETRYABLE_STATUS_CODES = {
        408,
        425,
        429,
        500,
        502,
        503,
        504,
    }

    def __init__(
        self,
        api_key=None,
        timeout_seconds=20,
        min_request_interval_seconds=3.0,
        max_attempts=5,
        session=None,
    ):
        self.api_key = (
            api_key
            or os.getenv("COINGECKO_API_KEY")
            or None
        )

        self.timeout_seconds = float(
            timeout_seconds
        )

        self.min_request_interval_seconds = (
            float(
                min_request_interval_seconds
            )
        )

        self.max_attempts = int(
            max_attempts
        )

        self.session = (
            session
            or requests.Session()
        )

        self.request_lock = threading.Lock()
        self.last_request_at = None

        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": (
                "multiservices-market-research/1.0"
            ),
        })

        if self.api_key:
            self.session.headers.update({
                "x-cg-demo-api-key": (
                    self.api_key
                ),
            })

    def get_coin_list(self):
        payload = self._get(
            "/coins/list",
            params={
                "include_platform": "false",
            },
        )

        if not isinstance(payload, list):
            raise ValueError(
                "CoinGecko coin list must be a list"
            )

        return payload

    def get_categories_list(self):
        payload = self._get(
            "/coins/categories/list"
        )

        if not isinstance(payload, list):
            raise ValueError(
                "CoinGecko categories list "
                "must be a list"
            )

        return payload
        
    def get_binance_futures_tickers(
        self,
        include_tickers="unexpired",
    ):
        payload = self._get(
            (
                "/derivatives/exchanges/"
                "binance_futures"
            ),
            params={
                "include_tickers": (
                    include_tickers
                ),
            },
        )

        if not isinstance(payload, dict):
            raise ValueError(
                "CoinGecko Binance Futures "
                "response must be a dict"
            )

        tickers = payload.get("tickers")

        if not isinstance(tickers, list):
            raise ValueError(
                "CoinGecko Binance Futures "
                "tickers must be a list"
            )

        return tickers

    def get_coins_by_category(
        self,
        category_id,
        vs_currency="usd",
        max_pages=25,
    ):
        category_id = str(
            category_id
        ).strip()

        if not category_id:
            raise ValueError(
                "category_id is required"
            )

        all_coins = []

        for page in range(
            1,
            max_pages + 1,
        ):
            payload = self._get(
                "/coins/markets",
                params={
                    "vs_currency": vs_currency,
                    "category": category_id,
                    "order": "market_cap_desc",
                    "per_page": 250,
                    "page": page,
                    "sparkline": "false",
                },
            )

            if not isinstance(payload, list):
                raise ValueError(
                    "CoinGecko category response "
                    "must be a list"
                )

            if not payload:
                break

            all_coins.extend(payload)

            if len(payload) < 250:
                break

        return all_coins

    def _get(
        self,
        path,
        params=None,
    ):
        url = (
            f"{self.BASE_URL}"
            f"{path}"
        )

        last_error = None

        for attempt in range(
            1,
            self.max_attempts + 1,
        ):
            self._wait_for_rate_limit()

            try:
                response = self.session.get(
                    url,
                    params=params,
                    timeout=self.timeout_seconds,
                )

            except requests.RequestException as exc:
                last_error = exc

                if attempt >= self.max_attempts:
                    break

                self._backoff(attempt)
                continue

            if response.status_code == 200:
                try:
                    return response.json()

                except ValueError as exc:
                    raise ValueError(
                        "CoinGecko returned "
                        "invalid JSON"
                    ) from exc

            response_error = RuntimeError(
                "CoinGecko request failed: "
                f"status={response.status_code} "
                f"path={path} "
                f"body={response.text[:300]}"
            )

            if (
                response.status_code
                not in self.RETRYABLE_STATUS_CODES
            ):
                raise response_error

            last_error = response_error

            if attempt >= self.max_attempts:
                break

            retry_after = response.headers.get(
                "Retry-After"
            )

            if retry_after:
                try:
                    wait_seconds = float(
                        retry_after
                    )
                except ValueError:
                    wait_seconds = None
            else:
                wait_seconds = None

            self._backoff(
                attempt,
                wait_seconds=wait_seconds,
            )

        raise RuntimeError(
            "CoinGecko request failed after "
            f"{self.max_attempts} attempts: "
            f"{last_error}"
        )

    def _wait_for_rate_limit(self):
        with self.request_lock:
            now = time.monotonic()

            if self.last_request_at is not None:
                elapsed = (
                    now
                    - self.last_request_at
                )

                remaining = (
                    self.min_request_interval_seconds
                    - elapsed
                )

                if remaining > 0:
                    time.sleep(remaining)

            self.last_request_at = (
                time.monotonic()
            )

    def _backoff(
        self,
        attempt,
        wait_seconds=None,
    ):
        if wait_seconds is None:
            wait_seconds = min(
                2 ** attempt,
                30,
            )

        time.sleep(
            max(float(wait_seconds), 1.0)
        )