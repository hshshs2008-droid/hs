import io
import json
import math
import threading
import unittest
from datetime import datetime, timezone
from email.message import Message
from email.utils import format_datetime
from http.client import BadStatusLine, HTTPException, IncompleteRead, RemoteDisconnected
from urllib.error import HTTPError, URLError
from unittest.mock import MagicMock, patch

import market


def row(index, opening="10", high="12", low="8", close="11"):
    start = index * market.FOUR_HOURS_MS
    return [start, opening, high, low, close, "1", start + market.FOUR_HOURS_MS - 1]


def response(data):
    return io.BytesIO(json.dumps(data).encode())


class MarketTests(unittest.TestCase):
    @patch("market.urlopen")
    def test_public_endpoints_have_no_authentication(self, urlopen):
        urlopen.side_effect = [
            response({"serverTime": 123456789}),
            response({"symbols": []}),
            response([row(0)]),
            response({"symbol": "BTCUSDT", "price": "67500.12000000"}),
        ]
        client = market.BinanceClient(base_url=market.DEFAULT_BASE_URL)
        self.assertEqual(client.server_time(), 123456789)
        self.assertEqual(client.spot_symbols(), [])
        self.assertEqual(len(client.candles("BTCUSDT", market.FOUR_HOURS_MS)), 1)
        self.assertEqual(client.latest_price("BTCUSDT"), "67500.12")
        urls = [call.args[0].full_url for call in urlopen.call_args_list]
        self.assertEqual(urls[0], "https://api.binance.com/api/v3/time")
        self.assertEqual(urls[1], "https://api.binance.com/api/v3/exchangeInfo?permissions=SPOT&showPermissionSets=false&symbolStatus=TRADING")
        self.assertEqual(urls[2], "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=4h&limit=500")
        self.assertEqual(urls[3], "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
        for call in urlopen.call_args_list:
            self.assertIsNone(call.args[0].data)
            self.assertIsNone(call.args[0].get_header("Authorization"))
            self.assertLessEqual(call.kwargs["timeout"], 20)

    @patch.object(market.BinanceClient, "_get")
    def test_latest_price_preserves_tiny_and_precise_quotes_without_float_rounding(self, get):
        client = market.BinanceClient()
        for raw, expected in (("0.000000010000", "0.00000001"),
                              ("25000.00000000", "25000"),
                              ("1.123456789123456789", "1.123456789123456789")):
            with self.subTest(raw=raw):
                get.return_value = {"symbol": "BTCUSDT", "price": raw}
                self.assertEqual(client.latest_price("BTCUSDT"), expected)
        self.assertEqual(get.call_count, 3)

    @patch.object(market.BinanceClient, "_get")
    def test_latest_price_rejects_invalid_values_and_mismatched_symbols(self, get):
        client = market.BinanceClient()
        for raw in (None, True, 10, "0", "-1", "NaN", "sNaN", "Infinity", "oops", "1e999999", "1e-999999"):
            with self.subTest(raw=raw), self.assertRaises(market.MarketError):
                get.return_value = {"symbol": "BTCUSDT", "price": raw}
                client.latest_price("BTCUSDT")
        get.return_value = {"symbol": "ETHUSDT", "price": "10"}
        with self.assertRaises(market.MarketError):
            client.latest_price("BTCUSDT")

    @patch.dict("os.environ", {"API_BINANCE_BASE": "https://data-api.binance.vision/"})
    @patch("market.urlopen", return_value=response({"serverTime": 42}))
    def test_configurable_https_base(self, urlopen):
        self.assertEqual(market.BinanceClient().server_time(), 42)
        self.assertEqual(urlopen.call_args.args[0].full_url, "https://data-api.binance.vision/api/v3/time")

    def test_rejects_nonpublic_origin_and_invalid_timeout(self):
        for base in ("http://api.binance.com", "https://key@api.binance.com", "https://api.binance.com/api", "https://api.binance.com?token=secret"):
            with self.subTest(base=base), self.assertRaises(market.MarketError):
                market.BinanceClient(base)
        for timeout in (0, 21, math.nan, True):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                market.BinanceClient(timeout=timeout)

    @patch.object(market.BinanceClient, "_get")
    def test_selects_trading_spot_usdt_pairs(self, get):
        good = {"symbol": "BTCUSDT", "status": "TRADING", "quoteAsset": "USDT", "isSpotTradingAllowed": True,
                "permissions": [], "permissionSets": []}
        get.return_value = {"symbols": [
            good, good,
            dict(good, symbol="ETHUSDT", status="BREAK"),
            dict(good, symbol="BTCFDUSD", quoteAsset="FDUSD"),
            dict(good, symbol="MARGINUSDT", isSpotTradingAllowed=False),
            dict(good, symbol="UNKNOWNUSDT", isSpotTradingAllowed="true"),
            dict(good, symbol="币安人生USDT"),
        ]}
        self.assertEqual(market.BinanceClient().spot_symbols(), ["BTCUSDT", "币安人生USDT"])

    @patch("market.MAX_RESPONSE_BYTES", 16)
    @patch("market.urlopen", return_value=response({"serverTime": 1234567890123456}))
    def test_response_limit_still_rejects_oversized_payload(self, urlopen):
        with self.assertRaisesRegex(market.MarketError, "oversized"):
            market.BinanceClient().server_time()

    @patch.object(market.BinanceClient, "_get")
    def test_excludes_unclosed_candles_and_exact_close_boundary(self, get):
        get.return_value = [row(0), row(1), row(2)]
        client = market.BinanceClient()
        closed = client.candles("BTCUSDT", 2 * market.FOUR_HOURS_MS)
        self.assertEqual([c.open_time for c in closed], [0, market.FOUR_HOURS_MS])
        self.assertEqual(closed[-1].close, 11)
        closed = client.candles("BTCUSDT", 2 * market.FOUR_HOURS_MS - 1)
        self.assertEqual([c.open_time for c in closed], [0])

    @patch.object(market.BinanceClient, "_get")
    def test_short_or_empty_histories_remain_available(self, get):
        client = market.BinanceClient()
        for rows, expected in (([], 0), ([row(0)], 1)):
            get.return_value = rows
            self.assertEqual(len(client.candles("NEWUSDT", market.FOUR_HOURS_MS)), expected)

    @patch.object(market.BinanceClient, "_get")
    def test_rejects_gaps_duplicates_unordered_and_wrong_intervals(self, get):
        bad_interval = row(0)
        bad_interval[6] += 1
        for rows in ([row(0), row(2)], [row(0), row(0)], [row(1), row(0)], [bad_interval]):
            with self.subTest(rows=rows), self.assertRaises(market.MarketError):
                get.return_value = rows
                market.BinanceClient().candles("BTCUSDT", 5 * market.FOUR_HOURS_MS)

    @patch.object(market.BinanceClient, "_get")
    def test_rejects_invalid_ohlc_and_shape(self, get):
        for rows in ([row(0, close="nan")], [row(0, low="inf")], [row(0, close="13")], [row(0, low="0")], [row(0, opening=None)], [[0, "1"]], {"code": -1}):
            with self.subTest(rows=rows), self.assertRaises(market.MarketError):
                get.return_value = rows
                market.BinanceClient().candles("BTCUSDT", market.FOUR_HOURS_MS)

    @patch("market.urlopen")
    def test_rate_limit_captures_retry_after_without_response_text(self, urlopen):
        for code in (429, 418):
            headers = Message()
            headers["Retry-After"] = "120"
            urlopen.side_effect = HTTPError("https://example.com/secret", code, "secret", headers, io.BytesIO(b"secret"))
            with self.subTest(code=code), self.assertRaises(market.MarketError) as raised:
                market.BinanceClient().server_time()
            self.assertEqual(raised.exception.status, code)
            self.assertEqual(raised.exception.retry_after, 300 if code == 418 else 120)
            self.assertNotIn("secret", str(raised.exception))

    @patch("market.urlopen")
    def test_network_errors_and_malformed_json_are_sanitized(self, urlopen):
        urlopen.side_effect = URLError("sensitive internal proxy details")
        with self.assertRaises(market.MarketError) as raised:
            market.BinanceClient().server_time()
        self.assertNotIn("sensitive", str(raised.exception))
        urlopen.side_effect = None
        urlopen.return_value = io.BytesIO(b"not-json-sensitive")
        with self.assertRaises(market.MarketError) as raised:
            market.BinanceClient().server_time()
        self.assertNotIn("sensitive", str(raised.exception))

    @patch.object(market.BinanceClient, "_get")
    def test_rejects_malformed_server_time_and_exchange_information(self, get):
        client = market.BinanceClient()
        for result in ({}, [], {"serverTime": True}, {"serverTime": "123"}, {"serverTime": -1}):
            with self.subTest(result=result), self.assertRaises(market.MarketError):
                get.return_value = result
                client.server_time()
        for result in ({}, [], {"symbols": [None]}, {"symbols": "BTCUSDT"}):
            with self.subTest(result=result), self.assertRaises(market.MarketError):
                get.return_value = result
                client.spot_symbols()

    @patch.object(market.BinanceClient, "_get")
    def test_rejects_invalid_call_parameters_before_network(self, get):
        client = market.BinanceClient()
        for symbol, timestamp, limit in (("BTC/USDT", 1, 500), ("BTCUSDT", 0, 500), ("BTCUSDT", 1, 1001)):
            with self.subTest(symbol=symbol, timestamp=timestamp, limit=limit), self.assertRaises(ValueError):
                client.candles(symbol, timestamp, limit)
        get.assert_not_called()


class SharedMarketGateTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.client = market.BinanceClient(clock=lambda: self.now)

    def http_error(self, status=429, retry_after=None):
        headers = Message()
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        return HTTPError("https://example.com/sensitive", status, "sensitive", headers,
                         io.BytesIO(b"sensitive"))

    @patch("market.urlopen")
    def test_all_endpoints_share_full_cooldown_and_resume_at_original_deadline(self, urlopen):
        urlopen.side_effect = [self.http_error(retry_after=10000),
                               response({"symbols": []}), response([row(0)]),
                               response({"serverTime": 42})]
        with self.assertRaises(market.MarketError) as initial:
            self.client.server_time()
        deadline = self.now + 10000
        self.assertEqual(initial.exception.cooldown_until, deadline)
        self.assertEqual(initial.exception.retry_after, 10000)
        self.now = deadline - 0.5
        for endpoint in (self.client.spot_symbols,
                         lambda: self.client.candles("BTCUSDT", market.FOUR_HOURS_MS),
                         lambda: self.client.latest_price("BTCUSDT"),
                         self.client.server_time):
            with self.subTest(endpoint=endpoint), self.assertRaises(market.MarketError) as blocked:
                endpoint()
            self.assertEqual(blocked.exception.status, 429)
            self.assertEqual(blocked.exception.retry_after, 0.5)
            self.assertEqual(blocked.exception.cooldown_until, deadline)
        self.assertEqual(urlopen.call_count, 1)
        self.now = deadline
        self.assertEqual(self.client.spot_symbols(), [])
        self.assertEqual(len(self.client.candles("BTCUSDT", market.FOUR_HOURS_MS)), 1)
        self.assertEqual(self.client.server_time(), 42)
        self.assertEqual(urlopen.call_count, 4)

    @patch("market.urlopen")
    def test_default_security_cooldowns_and_long_retry_after_are_preserved(self, urlopen):
        cases = ((429, None, 60), (418, None, 300), (403, None, 60),
                 (451, 120, 120), (418, 10000, 10000), (429, 10000, 10000))
        for status, retry_after, expected in cases:
            with self.subTest(status=status, retry_after=retry_after):
                client = market.BinanceClient(clock=lambda: self.now)
                urlopen.side_effect = [self.http_error(status, retry_after),
                                       response({"serverTime": 42})]
                urlopen.reset_mock()
                with self.assertRaises(market.MarketError) as initial:
                    client.server_time()
                deadline = self.now + expected
                self.assertEqual(initial.exception.retry_after, expected)
                self.assertEqual(initial.exception.cooldown_until, deadline)
                self.now = deadline - 1
                with self.assertRaises(market.MarketError) as blocked:
                    client.spot_symbols()
                self.assertEqual(blocked.exception.retry_after, 1)
                self.assertEqual(blocked.exception.cooldown_until, deadline)
                self.assertEqual(urlopen.call_count, 1)
                self.now = deadline
                self.assertEqual(client.server_time(), 42)
                self.assertEqual(urlopen.call_count, 2)

    @patch("market.urlopen")
    def test_http_date_retry_after_uses_injected_clock(self, urlopen):
        retry_at = format_datetime(datetime.fromtimestamp(self.now + 10000, timezone.utc), usegmt=True)
        urlopen.side_effect = self.http_error(retry_after=retry_at)
        with self.assertRaises(market.MarketError) as raised:
            self.client.server_time()
        self.assertEqual(raised.exception.retry_after, 10000)
        self.assertEqual(raised.exception.cooldown_until, self.now + 10000)

    @patch("market.urlopen")
    def test_waiting_thread_cannot_send_before_rate_limit_response_closes_gate(self, urlopen):
        entered = threading.Event()
        release = threading.Event()
        second_started = threading.Event()
        second_finished = threading.Event()
        outcomes = []
        blocked_response = MagicMock()
        blocked_response.__enter__.return_value = blocked_response

        def read_limited_response(*args):
            entered.set()
            if not release.wait(timeout=2):
                raise AssertionError("Test did not release the response.")
            raise self.http_error(retry_after=10000)

        blocked_response.read.side_effect = read_limited_response
        urlopen.return_value = blocked_response

        def first_worker():
            try:
                self.client.server_time()
            except Exception as error:
                outcomes.append(error)

        def second_worker():
            second_started.set()
            try:
                self.client.spot_symbols()
            except Exception as error:
                outcomes.append(error)
            finally:
                second_finished.set()

        first = threading.Thread(target=first_worker)
        second = threading.Thread(target=second_worker)
        first.start()
        try:
            self.assertTrue(entered.wait(timeout=1))
            second.start()
            self.assertTrue(second_started.wait(timeout=1))
            self.assertFalse(second_finished.wait(timeout=0.05))
            self.assertEqual(urlopen.call_count, 1)
        finally:
            release.set()
            first.join(timeout=2)
            if second.ident is not None:
                second.join(timeout=2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(len(outcomes), 2)
        self.assertTrue(all(isinstance(error, market.MarketError) for error in outcomes))
        self.assertEqual({error.cooldown_until for error in outcomes}, {self.now + 10000})
        self.assertEqual({error.status for error in outcomes}, {429})
        self.assertEqual(urlopen.call_count, 1)

    @patch("market.urlopen")
    def test_protocol_failures_are_sanitized_and_next_request_can_recover(self, urlopen):
        for protocol_error in (IncompleteRead(b"sensitive", 10),
                               RemoteDisconnected("sensitive"),
                               BadStatusLine("sensitive"), HTTPException("sensitive")):
            for stage in ("opening", "reading"):
                with self.subTest(error=type(protocol_error).__name__, stage=stage):
                    broken = MagicMock()
                    broken.__enter__.return_value = broken
                    broken.read.side_effect = protocol_error
                    urlopen.side_effect = [protocol_error if stage == "opening" else broken,
                                           response({"serverTime": 42})]
                    urlopen.reset_mock()
                    with self.assertRaises(market.MarketError) as raised:
                        self.client.server_time()
                    self.assertNotIn("sensitive", str(raised.exception))
                    self.assertIsNone(raised.exception.status)
                    self.assertIsNone(raised.exception.cooldown_until)
                    self.assertEqual(self.client.server_time(), 42)
                    self.assertEqual(urlopen.call_count, 2)

    @patch("market.urlopen")
    def test_non_rate_limit_http_failure_does_not_create_shared_cooldown(self, urlopen):
        urlopen.side_effect = [self.http_error(503, 10), response({"serverTime": 42})]
        with self.assertRaises(market.MarketError) as raised:
            self.client.server_time()
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(raised.exception.retry_after, 10)
        self.assertIsNone(raised.exception.cooldown_until)
        self.assertEqual(self.client.server_time(), 42)
        self.assertEqual(urlopen.call_count, 2)


if __name__ == "__main__":
    unittest.main()
