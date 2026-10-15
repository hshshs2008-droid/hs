"""Public Binance spot market data; no account keys are required."""

from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import json
import math
import os
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


FOUR_HOURS_MS = 4 * 60 * 60 * 1000
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
DEFAULT_BASE_URL = "https://api.binance.com"


class MarketError(Exception):
    """A sanitized market-data failure, with optional rate-limit metadata."""

    def __init__(self, message, retry_after=None, status=None, cooldown_until=None):
        super().__init__(message)
        self.retry_after = retry_after
        self.status = status
        self.cooldown_until = cooldown_until


@dataclass(frozen=True)
class Candle:
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float


def _retry_seconds(value, now=None):
    if not value:
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = when.timestamp() - (time.time() if now is None else now)
        except (ValueError, TypeError, OverflowError, OSError):
            return None
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


def _timestamp(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MarketError("Binance returned an invalid candle timestamp.")
    return value


def _price(value):
    if isinstance(value, bool):
        raise MarketError("Binance returned an invalid candle price.")
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        raise MarketError("Binance returned an invalid candle price.") from None
    if not math.isfinite(number) or number <= 0:
        raise MarketError("Binance returned an invalid candle price.")
    return number


def _valid_symbol(symbol):
    # Binance also lists symbols containing non-ASCII letters.
    return isinstance(symbol, str) and 1 <= len(symbol) <= 128 and symbol.isalnum()


class BinanceClient:
    def __init__(self, base_url=None, timeout=20, *, clock=time.time):
        base_url = base_url or os.environ.get("API_BINANCE_BASE", DEFAULT_BASE_URL)
        try:
            parts = urlsplit(base_url)
            valid = (
                parts.scheme == "https"
                and bool(parts.hostname)
                and parts.username is None
                and parts.password is None
                and parts.path in ("", "/")
                and not parts.query
                and not parts.fragment
                and (parts.port is None or 1 <= parts.port <= 65535)
            )
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise MarketError("API_BINANCE_BASE must be an HTTPS origin without credentials.")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 20:
            raise ValueError("Market request timeout must be greater than zero and at most 20 seconds.")
        if not callable(clock):
            raise ValueError("Market request clock must be callable.")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._clock = clock
        self._request_lock = threading.Lock()
        self._cooldown_until = 0.0
        self._cooldown_status = None

    def _get(self, path, params=None):
        url = self.base_url + path
        if params:
            url += "?" + urlencode(params)
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "SpotAlerts/1.0"})
        # Both workers share this client. Hold the gate through response/error
        # handling so a concurrent request cannot slip past a new cooldown.
        with self._request_lock:
            now = self._clock()
            if now < self._cooldown_until:
                raise MarketError(
                    "Binance requests are waiting for the exchange cooldown.",
                    retry_after=self._cooldown_until - now,
                    status=self._cooldown_status,
                    cooldown_until=self._cooldown_until,
                )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    data = response.read(MAX_RESPONSE_BYTES + 1)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise MarketError("Binance returned an oversized response.")
                result = json.loads(data)
            except HTTPError as error:
                now = self._clock()
                retry_after = _retry_seconds(
                    error.headers.get("Retry-After") if error.headers else None, now
                )
                cooldown_until = None
                if error.code in (418, 429, 403, 451):
                    retry_after = max(300 if error.code == 418 else 60, retry_after or 0)
                    cooldown_until = now + retry_after
                    self._cooldown_until = cooldown_until
                    self._cooldown_status = error.code
                raise MarketError(
                    f"Binance HTTP status: {error.code}", retry_after=retry_after,
                    status=error.code, cooldown_until=cooldown_until,
                ) from None
            except (URLError, TimeoutError, OSError, HTTPException):
                raise MarketError("Unable to reach Binance market data. Check network access.") from None
            except (ValueError, UnicodeError):
                raise MarketError("Binance returned invalid JSON.") from None
            return result

    def server_time(self):
        result = self._get("/api/v3/time")
        value = result.get("serverTime") if isinstance(result, dict) else None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise MarketError("Binance returned an invalid server time.")
        return value

    def spot_symbols(self):
        # Permission sets are unnecessary for public spot scanning and can
        # make the unfiltered exchange metadata exceed the response limit.
        result = self._get("/api/v3/exchangeInfo", {
            "permissions": "SPOT",
            "showPermissionSets": "false",
            "symbolStatus": "TRADING",
        })
        rows = result.get("symbols") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            raise MarketError("Binance returned invalid exchange information.")
        symbols = set()
        for row in rows:
            if not isinstance(row, dict):
                raise MarketError("Binance returned invalid exchange information.")
            if (
                row.get("status") == "TRADING"
                and row.get("isSpotTradingAllowed") is True
                and row.get("quoteAsset") == "USDT"
            ):
                symbol = row.get("symbol")
                if not _valid_symbol(symbol):
                    raise MarketError("Binance returned an invalid spot symbol.")
                symbols.add(symbol)
        return sorted(symbols)

    def candles(self, symbol, server_time_ms, limit=500):
        """Return contiguous closed UTC 4h candles; short histories stay short."""
        if not _valid_symbol(symbol):
            raise ValueError("Invalid Binance symbol.")
        if isinstance(server_time_ms, bool) or not isinstance(server_time_ms, int) or server_time_ms <= 0:
            raise ValueError("A positive Binance server timestamp is required.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("Candle limit must be between 1 and 1000.")
        rows = self._get("/api/v3/klines", {"symbol": symbol, "interval": "4h", "limit": limit})
        if not isinstance(rows, list):
            raise MarketError("Binance returned invalid candle data.")
        candles = []
        previous_open_time = None
        for row in rows:
            if not isinstance(row, list) or len(row) < 7:
                raise MarketError("Binance returned an invalid candle row.")
            open_time, close_time = _timestamp(row[0]), _timestamp(row[6])
            if open_time % FOUR_HOURS_MS or close_time != open_time + FOUR_HOURS_MS - 1:
                raise MarketError("Binance returned an invalid 4h candle interval.")
            if previous_open_time is not None and open_time != previous_open_time + FOUR_HOURS_MS:
                raise MarketError("Binance candle history has gaps or out-of-order rows.")
            previous_open_time = open_time
            opening, high, low, close = (_price(value) for value in row[1:5])
            if low > min(opening, close) or high < max(opening, close) or low > high:
                raise MarketError("Binance returned inconsistent candle prices.")
            if close_time < server_time_ms:
                candles.append(Candle(open_time, close_time, opening, high, low, close))
        return candles
