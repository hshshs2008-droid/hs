"""Public Gate.io Spot data for USDT coins absent from active Binance Spot.

Gate's official SDK documents /spot/time server_time in milliseconds and
/spot/candlesticks as timestamp, quote volume, close, high, low, open,
optional base volume and optional closed flag. Requests use limit alone:
Gate explicitly disallows combining limit with from/to.
"""
import argparse
from decimal import Decimal, InvalidOperation
from http.client import HTTPException
import json
import math
import os
import sys
import threading
import time
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from market import BinanceClient, Candle, FOUR_HOURS_MS, MAX_RESPONSE_BYTES, MarketError, _retry_seconds, EXCLUDED_BASE_ASSETS


DEFAULT_GATE_BASE = 'https://api.gateio.ws'
MAX_GATE_SECONDS = 32_503_680_000  # Reject millisecond values in second-only fields.


def valid_gate_pair(symbol):
    if not isinstance(symbol, str) or not 6 <= len(symbol) <= 128 or not symbol.endswith('_USDT'):
        return False
    base = symbol[:-5]
    return bool(base) and base.replace('_', '').replace('-', '').replace('.', '').isalnum()


def _positive_price(value):
    if isinstance(value, bool):
        raise MarketError('Gate.io returned an invalid candle price.')
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise MarketError('Gate.io returned an invalid candle price.') from None
    if not math.isfinite(number) or number <= 0:
        raise MarketError('Gate.io returned an invalid candle price.')
    return number


def _volume(value):
    if isinstance(value, bool):
        raise MarketError('Gate.io returned an invalid candle volume.')
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise MarketError('Gate.io returned an invalid candle volume.') from None
    if not number.is_finite() or number < 0:
        raise MarketError('Gate.io returned an invalid candle volume.')


def _open_time(value):
    if type(value) is int:
        seconds = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 11:
        seconds = int(value)
    else:
        raise MarketError('Gate.io returned an invalid candle timestamp.')
    if not 0 <= seconds <= MAX_GATE_SECONDS:
        raise MarketError('Gate.io returned an invalid candle timestamp unit.')
    timestamp = seconds * 1000
    if timestamp % FOUR_HOURS_MS:
        raise MarketError('Gate.io returned an invalid 4h candle interval.')
    return timestamp


def _is_closed_flag(value):
    return type(value) is bool or isinstance(value, str) and value in ('true', 'false')


def _closed_flag(value):
    if not _is_closed_flag(value):
        raise MarketError('Gate.io returned an invalid candle closed flag.')
    return value is True or value == 'true'


class GateClient:
    """Gate requests have their own shared gate and cooldown, independent of Binance."""

    # A valid nonempty upstream list can yield zero pairs after exclusions.
    allow_empty_universe = True

    def __init__(self, binance_client, base_url=DEFAULT_GATE_BASE, timeout=20, *, clock=time.time):
        try:
            parts = urlsplit(base_url)
            valid = (parts.scheme == 'https' and bool(parts.hostname) and parts.username is None
                     and parts.password is None and parts.path in ('', '/')
                     and not parts.query and not parts.fragment
                     and (parts.port is None or 1 <= parts.port <= 65535))
        except (TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise MarketError('API_GATE_BASE must be an HTTPS origin without credentials.')
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 20:
            raise ValueError('Market request timeout must be greater than zero and at most 20 seconds.')
        if not callable(clock):
            raise ValueError('Market request clock must be callable.')
        if not callable(getattr(binance_client, 'spot_symbols', None)):
            raise ValueError('A Binance universe client is required.')
        self.binance_client = binance_client
        self.base_url = base_url.rstrip('/')
        self.timeout, self._clock = timeout, clock
        self._request_lock = threading.Lock()
        self._universe_lock = threading.Lock()
        self._eligible_symbols = frozenset()
        self._cooldown_until = 0.0
        self._cooldown_status = None
        self.selection_path = None
        self._selection_lock = threading.Lock()

    def _select_fixed(self, eligible):
        """Persist a fixed top 800; checkpoint discovery so restarts resume safely.

        Rank by median USDT volume of seven complete UTC days. New listings
        lacking all seven days are excluded. No absolute volume floor is used.
        """
        if self.selection_path is None:
            return eligible
        with self._selection_lock:
            Path(self.selection_path).parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.selection_path, timeout=20)) as db:
                db.execute('CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS scores (symbol TEXT PRIMARY KEY, volume TEXT NOT NULL)')
                saved = db.execute("SELECT value FROM config WHERE key='selected'").fetchone()
                if saved:
                    return set(json.loads(saved[0])) & eligible
                anchor = db.execute("SELECT value FROM config WHERE key='day'").fetchone()
                if anchor is None:
                    day = self.server_time() // 86400000 * 86400
                    with db:
                        db.execute("INSERT INTO config VALUES ('day', ?)", (str(day),))
                        db.execute("INSERT INTO config VALUES ('candidates', ?)", (json.dumps(sorted(eligible)),))
                else:
                    day = int(anchor[0])
                candidates = json.loads(db.execute("SELECT value FROM config WHERE key='candidates'").fetchone()[0])
                done = dict(db.execute('SELECT symbol, volume FROM scores'))
                expected = set(range(day - 7 * 86400, day, 86400))
                for symbol in candidates:
                    if symbol in done or symbol not in eligible:
                        continue
                    time.sleep(0.15)
                    rows = self._get('/spot/candlesticks', {'currency_pair': symbol, 'interval': '1d',
                                    'from': day - 7 * 86400, 'to': day - 1})
                    if not isinstance(rows, list):
                        raise MarketError('Invalid Gate daily volume history.')
                    volumes = {}
                    for row in rows:
                        if not isinstance(row, list) or len(row) not in (6, 7, 8):
                            raise MarketError('Invalid Gate daily volume row.')
                        try:
                            stamp = int(row[0])
                        except (ValueError, TypeError):
                            raise MarketError('Invalid Gate daily timestamp.') from None
                        if stamp not in expected or stamp in volumes:
                            raise MarketError('Unexpected Gate daily timestamp.')
                        _volume(row[1])
                        if len(row) == 8 and not _closed_flag(row[7]):
                            continue
                        if len(row) == 7 and _is_closed_flag(row[6]) and not _closed_flag(row[6]):
                            continue
                        volumes[stamp] = Decimal(str(row[1]))
                    median = sorted(volumes.values())[3] if set(volumes) == expected else Decimal(0)
                    with db:
                        db.execute('INSERT INTO scores VALUES (?, ?)', (symbol, str(median)))
                    done[symbol] = str(median)
                ranked = sorted((s for s in candidates if s in eligible and Decimal(done.get(s, '0')) > 0),
                                key=lambda s: (-Decimal(done[s]), s))[:800]
                if not ranked:
                    raise MarketError('No Gate pairs have a positive seven-day median volume.')
                with db:
                    db.execute("INSERT INTO config VALUES ('selected', ?)", (json.dumps(ranked),))
                return set(ranked)

    def _get(self, path, params=None):
        url = self.base_url + '/api/v4' + path
        if params:
            url += '?' + urlencode(params)
        request = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'SpotAlerts/1.0'})
        with self._request_lock:
            now = self._clock()
            if now < self._cooldown_until:
                raise MarketError('Gate.io requests are waiting for the exchange cooldown.',
                                  retry_after=self._cooldown_until - now,
                                  status=self._cooldown_status, cooldown_until=self._cooldown_until)
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise MarketError('Gate.io returned an oversized response.')
                result = json.loads(raw)
            except HTTPError as error:
                now = self._clock()
                retry_after = _retry_seconds(error.headers.get('Retry-After') if error.headers else None, now)
                deadline = None
                if error.code in (418, 429, 403, 451):
                    retry_after = max(300 if error.code == 418 else 60, retry_after or 0)
                    deadline = now + retry_after
                    self._cooldown_until, self._cooldown_status = deadline, error.code
                raise MarketError(f'Gate.io HTTP status: {error.code}', retry_after=retry_after,
                                  status=error.code, cooldown_until=deadline) from None
            except (URLError, TimeoutError, OSError, HTTPException):
                raise MarketError('Unable to reach Gate.io market data. Check network access.') from None
            except (ValueError, UnicodeError):
                raise MarketError('Gate.io returned invalid JSON.') from None
            return result

    def server_time(self):
        result = self._get('/spot/time')
        if not isinstance(result, dict):
            raise MarketError('Gate.io returned an invalid server time.')
        # Current SDK specifies milliseconds. Older responses exposed seconds,
        # occasionally alongside server_time_ms; normalize those explicitly.
        value = result.get('server_time_ms', result.get('server_time'))
        if type(value) is not int:
            raise MarketError('Gate.io returned an invalid server time.')
        if 'server_time_ms' in result:
            milliseconds = value
        elif 1_000_000_000 <= value <= MAX_GATE_SECONDS:
            milliseconds = value * 1000
        else:
            milliseconds = value
        if not 946_684_800_000 <= milliseconds <= MAX_GATE_SECONDS * 1000:
            raise MarketError('Gate.io returned an invalid server time unit.')
        return milliseconds

    def spot_symbols(self):
        # Refresh the exclusion set at every universe refresh. Failure must not
        # accidentally turn this source into all Gate.io pairs.
        binance = self.binance_client.spot_symbols()
        if (not isinstance(binance, (list, tuple)) or not binance
                or not all(isinstance(symbol, str) and symbol.endswith('USDT')
                           and symbol.isalnum() for symbol in binance)):
            raise MarketError('The Binance exclusion universe is unavailable for Gate.io.')
        excluded = {symbol[:-4].upper() for symbol in binance} | EXCLUDED_BASE_ASSETS
        result = self._get('/spot/currency_pairs')
        if not isinstance(result, list) or not result:
            raise MarketError('Gate.io returned invalid or empty exchange information.')
        selected = set()
        for pair in result:
            if not isinstance(pair, dict):
                raise MarketError('Gate.io returned invalid exchange information.')
            if (pair.get('quote') != 'USDT' or pair.get('trade_status') != 'tradable'
                    or pair.get('type', 'normal') != 'normal'):
                continue
            symbol, base = pair.get('id'), pair.get('base')
            if (not valid_gate_pair(symbol) or not isinstance(base, str)
                    or symbol != base + '_USDT'):
                raise MarketError('Gate.io returned an invalid spot pair identifier.')
            if base.upper() not in excluded:
                selected.add(symbol)
        selected = self._select_fixed(selected)
        with self._universe_lock:
            self._eligible_symbols = frozenset(selected)
        return sorted(selected)

    def is_eligible(self, symbol):
        """Read the last successful live universe snapshot without network I/O."""
        if not valid_gate_pair(symbol):
            return False
        with self._universe_lock:
            return symbol in self._eligible_symbols

    def latest_price(self, symbol):
        if not valid_gate_pair(symbol):
            raise ValueError('Invalid Gate.io pair.')
        result = self._get('/spot/tickers', {'currency_pair': symbol})
        if (not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict)
                or result[0].get('currency_pair') != symbol):
            raise MarketError('Gate.io returned an invalid latest price.')
        raw = result[0].get('last')
        if not isinstance(raw, str) or not 1 <= len(raw) <= 80:
            raise MarketError('Gate.io returned an invalid latest price.')
        try:
            price = Decimal(raw)
        except InvalidOperation:
            raise MarketError('Gate.io returned an invalid latest price.') from None
        if not price.is_finite() or price <= 0 or not -40 <= price.adjusted() <= 30:
            raise MarketError('Gate.io returned an invalid latest price.')
        plain = format(price, 'f')
        return plain.rstrip('0').rstrip('.') if '.' in plain else plain

    def candles(self, symbol, server_time_ms, limit=500):
        if not valid_gate_pair(symbol):
            raise ValueError('Invalid Gate.io pair.')
        if type(server_time_ms) is not int or server_time_ms <= 0:
            raise ValueError('A positive Gate.io server timestamp is required.')
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Candle limit must be between 1 and 1000.')
        rows = self._get('/spot/candlesticks', {'currency_pair': symbol, 'interval': '4h', 'limit': limit})
        if not isinstance(rows, list) or len(rows) > limit:
            raise MarketError('Gate.io returned invalid candle data.')
        parsed = []
        seen = set()
        for row in rows:
            if not isinstance(row, list) or len(row) not in (6, 7, 8):
                raise MarketError('Gate.io returned an invalid candle row.')
            opened = _open_time(row[0])
            if opened in seen:
                raise MarketError('Gate.io returned duplicate candle timestamps.')
            seen.add(opened)
            _volume(row[1])
            close, high, low, opening = (_positive_price(value) for value in row[2:6])
            if low > min(opening, close) or high < max(opening, close) or low > high:
                raise MarketError('Gate.io returned inconsistent candle prices.')
            closed = True
            if len(row) == 8:
                _volume(row[6])
                closed = _closed_flag(row[7])
            elif len(row) == 7:
                if _is_closed_flag(row[6]):
                    closed = _closed_flag(row[6])
                else:
                    _volume(row[6])
            ended = opened + FOUR_HOURS_MS - 1
            if closed and ended < server_time_ms:
                parsed.append(Candle(opened, ended, opening, high, low, close))
        parsed.sort(key=lambda candle: candle.open_time)
        if any(current.open_time != previous.open_time + FOUR_HOURS_MS
               for previous, current in zip(parsed, parsed[1:])):
            raise MarketError('Gate.io closed candle history has gaps.')
        return parsed


def main(argv=None):
    """Explicit, read-only schema/connection probe; never starts the bot."""
    parser = argparse.ArgumentParser(description='Check public Gate.io Spot data without starting the bot.')
    parser.add_argument('--check', action='store_true', help='verify time, pair discovery, ticker and 4h candles')
    arguments = parser.parse_args(argv)
    if not arguments.check:
        parser.print_help()
        return 0
    stage = 'configuration'
    try:
        client = GateClient(BinanceClient(), base_url=os.environ.get('API_GATE_BASE') or DEFAULT_GATE_BASE)
        stage = 'server_time'
        server_time_ms = client.server_time()
        stage = 'eligible_pairs'
        symbols = client.spot_symbols()
        stage = 'ticker'
        client.latest_price('BTC_USDT')
        stage = 'candles'
        candles = client.candles('BTC_USDT', server_time_ms, 30)
        target = ((server_time_ms - 15_000) // FOUR_HOURS_MS) * FOUR_HOURS_MS - 1
        if len(candles) < 20 or not any(candle.close_time == target for candle in candles):
            raise MarketError('Gate.io check requires recent contiguous closed 4h sample candles.')
        print(f'GATE_OK eligible_spot_usdt_pairs={len(symbols)} '
              f'sample_closed_4h_candles={len(candles)} ticker_verified=BTC_USDT')
        return 0
    except Exception as error:
        status = getattr(error, 'status', None)
        if type(status) is not int or not 100 <= status <= 599:
            status = None
        print(f'GATE_CHECK_FAILED error_type={type(error).__name__} http_status={status} stage={stage}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
