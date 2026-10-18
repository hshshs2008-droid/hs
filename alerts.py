"""Durable, opt-in delivery of closed-candle Binance spot signals."""

from datetime import datetime
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo

from indicators import entry_signal
from market import FOUR_HOURS_MS, MarketError


CLOSE_GRACE_MS = 15_000
DELIVERY_SPACING_SECONDS = 1.05
DISPLAY_TZ = ZoneInfo("Asia/Jerusalem")
logger = logging.getLogger(__name__)


class AlertDeliveryError(Exception):
    """Expected, sanitized Telegram delivery failure; the message stays queued."""

    def __init__(self, message, retry_after=None, status=None):
        super().__init__(message)
        self.retry_after = retry_after
        self.status = status


def _close_label(close_time):
    # Binance timestamps the last millisecond, rather than the next boundary.
    return datetime.fromtimestamp((close_time + 1) / 1000, DISPLAY_TZ).strftime("%Y-%m-%d (%H:%M)")


def _refresh_alert_times(message, close_time, rsi_event_close_time, stoch_event_close_time):
    """Refresh queued messages without rewriting stored indicators or events."""
    timestamps = (
        ("إغلاق الإشارة: ", "إغلاق شمعة 4h: ", "🕒 ", close_time),
        ("عبور RSI فوق 20: ", "عبور RSI فوق 20: ", "↗️ ", rsi_event_close_time),
        ("تقاطع K فوق D: ", "تقاطع K فوق D: ", "🔀 ", stoch_event_close_time),
    )
    styles = {"RSI (14): ": "📊 ", "K: ": "🔹 ", "D: ": "🔸 ", "الشرطان ": "🔗 "}
    lines = []
    for line in message.splitlines():
        if line == "قيم آخر إغلاق:":
            continue
        if line == "بايننس سبوت • فريم 4 ساعات • شموع مغلقة":
            lines.extend(("🏷️ Binance Spot", "⏱️ 4h • شموع مغلقة"))
            continue
        if line.startswith("🔔 تحقق فلتر الدخول: "):
            line = "📈 " + line.removeprefix("🔔 تحقق فلتر الدخول: ")
        elif line.startswith("🔔 "):
            line = "📈 " + line.removeprefix("🔔 ")
        for prefix, label, icon, timestamp in timestamps:
            if line.startswith((prefix, icon + prefix, label, icon + label)):
                line = icon + label + _close_label(timestamp)
                break
        for prefix, icon in styles.items():
            if line.startswith(prefix):
                line = icon + line
                break
        lines.append(line)
    return "\n".join(lines)


def format_alert(symbol, signal):
    timing = "الشرطان في الشمعة نفسها."
    if signal.rsi_event_close_time != signal.stoch_event_close_time:
        timing = "الشرطان في شمعتين متتاليتين."
    message = (
        f"📈 {symbol}\n"
        "بايننس سبوت • فريم 4 ساعات • شموع مغلقة\n"
        f"إغلاق شمعة 4h: {_close_label(signal.close_time)}\n"
        f"قيم آخر إغلاق:\n"
        f"RSI (14): {signal.rsi_previous:.2f} → {signal.rsi_current:.2f}\n"
        f"K: {signal.k_previous:.2f} → {signal.k_current:.2f}\n"
        f"D: {signal.d_previous:.2f} → {signal.d_current:.2f}\n"
        f"عبور RSI فوق 20: {_close_label(signal.rsi_event_close_time)}\n"
        f"تقاطع K فوق D: {_close_label(signal.stoch_event_close_time)}\n"
        f"{timing}"
    )
    return _refresh_alert_times(message, signal.close_time,
                                signal.rsi_event_close_time, signal.stoch_event_close_time)


class AlertStore:
    """SQLite state shared by the polling thread and the market worker.

    A lock protects the shared connection, including transaction boundaries.
    Delivery is at least once: successful responses are recorded, but a crash
    after Telegram accepts a message and before commit can repeat that message.
    """

    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA busy_timeout = 10000")
        with self._db:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    chat_id INTEGER PRIMARY KEY,
                    activation_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    symbol TEXT PRIMARY KEY,
                    close_time INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS signals (
                    symbol TEXT NOT NULL,
                    close_time INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    rsi_event_close_time INTEGER NOT NULL,
                    stoch_event_close_time INTEGER NOT NULL,
                    PRIMARY KEY (symbol, close_time)
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    chat_id INTEGER NOT NULL REFERENCES subscriptions(chat_id) ON DELETE CASCADE,
                    symbol TEXT NOT NULL,
                    close_time INTEGER NOT NULL,
                    sent_at_ms INTEGER,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (chat_id, symbol, close_time),
                    FOREIGN KEY (symbol, close_time) REFERENCES signals(symbol, close_time)
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)

    def close(self):
        with self._lock:
            self._db.close()

    def subscribe(self, chat_id, activation_ms):
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO subscriptions(chat_id, activation_ms) VALUES (?, ?)",
                (chat_id, activation_ms),
            )
            return cursor.rowcount == 1

    def unsubscribe(self, chat_id):
        with self._lock, self._db:
            cursor = self._db.execute("DELETE FROM subscriptions WHERE chat_id = ?", (chat_id,))
            return cursor.rowcount == 1

    def subscribers(self):
        with self._lock:
            return self._db.execute(
                "SELECT chat_id, activation_ms FROM subscriptions ORDER BY chat_id"
            ).fetchall()

    def checkpoint(self, symbol):
        with self._lock:
            row = self._db.execute(
                "SELECT close_time FROM checkpoints WHERE symbol = ?", (symbol,)
            ).fetchone()
            return row[0] if row else None

    def record(self, symbol, checked_close_time, signal=None):
        """Save an evaluated candle and its eligible deliveries atomically."""
        if signal is not None and signal.close_time != checked_close_time:
            raise ValueError("Signal and checked candle must share a close timestamp.")
        with self._lock, self._db:
            if signal is not None:
                self._db.execute(
                    "INSERT OR IGNORE INTO signals VALUES (?, ?, ?, ?, ?)",
                    (symbol, signal.close_time, format_alert(symbol, signal),
                     signal.rsi_event_close_time, signal.stoch_event_close_time),
                )
                self._db.execute(
                    """INSERT OR IGNORE INTO deliveries(chat_id, symbol, close_time)
                       SELECT chat_id, ?, ? FROM subscriptions WHERE activation_ms <= ?""",
                    (symbol, signal.close_time, signal.close_time),
                )
            self._db.execute(
                """INSERT INTO checkpoints(symbol, close_time) VALUES (?, ?)
                   ON CONFLICT(symbol) DO UPDATE SET close_time = MAX(close_time, excluded.close_time)""",
                (symbol, checked_close_time),
            )

    def pending(self, symbol=None):
        query = """SELECT d.chat_id, d.symbol, d.close_time, s.message,
                          s.rsi_event_close_time, s.stoch_event_close_time
                   FROM deliveries AS d JOIN signals AS s
                   ON d.symbol = s.symbol AND d.close_time = s.close_time
                   WHERE d.sent_at_ms IS NULL"""
        parameters = ()
        if symbol is not None:
            query += " AND d.symbol = ?"
            parameters = (symbol,)
        query += " ORDER BY d.close_time, d.symbol, d.chat_id"
        with self._lock:
            rows = self._db.execute(query, parameters).fetchall()
        return [(chat_id, symbol, close_time,
                 _refresh_alert_times(message, close_time, rsi_time, stoch_time))
                for chat_id, symbol, close_time, message, rsi_time, stoch_time in rows]

    def is_pending(self, chat_id, symbol, close_time):
        with self._lock:
            return self._db.execute(
                """SELECT 1 FROM deliveries WHERE chat_id = ? AND symbol = ?
                   AND close_time = ? AND sent_at_ms IS NULL""",
                (chat_id, symbol, close_time),
            ).fetchone() is not None

    def delivery_attempt(self, chat_id, symbol, close_time, sent_at_ms=None):
        with self._lock, self._db:
            self._db.execute(
                """UPDATE deliveries SET attempts = attempts + 1, sent_at_ms = ?
                   WHERE chat_id = ? AND symbol = ? AND close_time = ? AND sent_at_ms IS NULL""",
                (sent_at_ms, chat_id, symbol, close_time),
            )

    def _metadata(self, key, default):
        with self._lock:
            row = self._db.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def _set_metadata(self, key, value):
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def status(self):
        status = self._metadata("status", {"state": "idle", "symbols_checked": 0, "alerts_sent": 0,
                                            "pending": 0, "blocked_until": 0, "error": None})
        status["delivery_blocked_until"] = self.get_delivery_blocked_until()
        return status

    def set_status(self, status):
        self._set_metadata("status", status)

    def get_history_cache(self):
        try:
            return self._metadata("history_reports", {})
        except (ValueError, TypeError):
            # A corrupt report must not prevent the next refresh replacing it.
            return {}

    def save_history_cache(self, kind, payload):
        if kind not in ("complete", "partial"):
            raise ValueError("Invalid history cache kind.")
        with self._lock:
            reports = self.get_history_cache()
            if not isinstance(reports, dict):
                reports = {}
            reports[kind] = payload
            self._set_metadata("history_reports", reports)

    def get_update_offset(self):
        return self._metadata("update_offset", 0)

    def set_update_offset(self, offset):
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("Telegram update offset must be a nonnegative integer.")
        with self._lock:
            self._set_metadata("update_offset", max(self.get_update_offset(), offset))

    def get_delivery_blocked_until(self):
        return self._metadata("delivery_blocked_until", 0)

    def set_delivery_blocked_until(self, timestamp):
        with self._lock:
            self._set_metadata("delivery_blocked_until", max(self.get_delivery_blocked_until(), timestamp))

    def get_next_delivery_at(self):
        return self._metadata("next_delivery_at", 0)

    def set_next_delivery_at(self, timestamp):
        self._set_metadata("next_delivery_at", timestamp)


class Scanner:
    def __init__(self, client, store, send_alert, *, poll_seconds=60,
                 request_delay=0.15, clock=time.time, sleep=time.sleep):
        if poll_seconds <= 0 or request_delay < 0:
            raise ValueError("Polling must be positive and request delay nonnegative.")
        self.client = client
        self.store = store
        self.send_alert = send_alert
        self.poll_seconds = poll_seconds
        self.request_delay = request_delay
        self.clock = clock
        self.sleep = sleep
        self._symbols = None
        self._symbols_expires = 0
        self._stop = None

    def _request(self, method, *args):
        # All market requests are serial and spaced; never launch a request burst.
        if self.request_delay:
            if self._stop is not None:
                self._stop.wait(self.request_delay)
            else:
                self.sleep(self.request_delay)
        return method(*args)

    def _deliver(self, rows, attempted):
        sent = failed = 0
        if self.clock() < self.store.get_delivery_blocked_until():
            return sent, failed
        for chat_id, symbol, close_time, message in rows:
            key = (chat_id, symbol, close_time)
            if key in attempted or not self.store.is_pending(*key):
                continue
            # One global alert queue, paced even across symbols, chats, retries
            # and process restarts. A cooldown never blocks market collection.
            delay = self.store.get_next_delivery_at() - self.clock()
            if delay > 0:
                if self._stop is not None:
                    if self._stop.wait(delay):
                        break
                else:
                    self.sleep(delay)
            if self._stop is not None and self._stop.is_set():
                break
            if not self.store.is_pending(*key):
                continue
            attempted.add(key)
            self.store.set_next_delivery_at(self.clock() + DELIVERY_SPACING_SECONDS)
            try:
                self.send_alert(chat_id, message)
            except AlertDeliveryError as error:
                self.store.delivery_attempt(*key)
                failed += 1
                if error.status == 429:
                    blocked_until = self.clock() + max(60, error.retry_after or 0)
                    self.store.set_delivery_blocked_until(blocked_until)
                    status = self.store.status()
                    status.update(delivery_blocked_until=blocked_until,
                                  delivery_warning="إرسال التنبيهات ينتظر انتهاء مهلة تيليجرام.")
                    self.store.set_status(status)
                    break
            else:
                self.store.delivery_attempt(*key, sent_at_ms=int(self.clock() * 1000))
                sent += 1
        return sent, failed

    def _finish(self, status):
        status["pending"] = len(self.store.pending())
        status["delivery_blocked_until"] = self.store.get_delivery_blocked_until()
        warning = None
        if self.clock() < status["delivery_blocked_until"] and status["pending"]:
            warning = "إرسال التنبيهات ينتظر انتهاء مهلة تيليجرام."
        elif status["pending"]:
            warning = "بعض التنبيهات تنتظر إعادة محاولة إرسالها."
        status["delivery_warning"] = warning
        if warning and not status.get("error"):
            status["error"] = warning
        status["heartbeat_ms"] = int(self.clock() * 1000)
        self.store.set_status(status)
        return status

    def _block(self, error, status):
        default = 300 if error.status == 418 else 60
        # Do not cap a long Retry-After: the full exchange cooldown applies.
        deadline = getattr(error, "cooldown_until", None)
        if deadline is None:
            deadline = self.clock() + max(default, error.retry_after or 0)
        status.update(state="blocked", blocked_until=deadline,
                      error="تعذر جلب بيانات بايننس." +
                      (f" HTTP {error.status}." if error.status is not None else ""))
        return self._finish(status)

    def scan_once(self):
        previous = self.store.status()
        status = {"state": "scanning", "symbols_checked": 0, "alerts_sent": 0,
                  "pending": 0, "blocked_until": previous.get("blocked_until", 0),
                  "error": None, "last_scan_ms": int(self.clock() * 1000), "requests": 0,
                  "heartbeat_ms": int(self.clock() * 1000),
                  "last_error_ms": previous.get("last_error_ms"), "retry_at_ms": 0}
        subscribers = self.store.subscribers()
        if not subscribers:
            status["state"] = "idle"
            return self._finish(status)

        attempted = set()
        status["alerts_sent"], delivery_failed = self._deliver(self.store.pending(), attempted)
        if self.clock() < status["blocked_until"]:
            status.update(state="backoff", error=previous.get("error"))
            return self._finish(status)

        status["blocked_until"] = 0
        self.store.set_status(status)
        try:
            server_time = self._request(self.client.server_time)
            status["requests"] += 1
            target = ((server_time - CLOSE_GRACE_MS) // FOUR_HOURS_MS) * FOUR_HOURS_MS - 1
            status["target_close_time"] = target
            if self._symbols is None or self.clock() >= self._symbols_expires:
                self._symbols = self._request(self.client.spot_symbols)
                status["requests"] += 1
                self._symbols_expires = self.clock() + 1800
        except MarketError as error:
            return self._block(error, status)

        status["symbols_total"] = len(self._symbols)
        if not self._symbols:
            return self._block(MarketError("No active Binance spot USDT pairs."), status)
        earliest_activation = min(activation for _, activation in subscribers)
        market_failed = False
        for symbol in self._symbols:
            if self._stop is not None and self._stop.is_set():
                break
            checkpoint = self.store.checkpoint(symbol)
            if checkpoint is not None and checkpoint >= target:
                status["symbols_checked"] += 1
                continue
            try:
                candles = self._request(self.client.candles, symbol, server_time, 500)
                status["requests"] += 1
            except MarketError as error:
                if error.status in (418, 429, 403, 451):
                    return self._block(error, status)
                market_failed = True
                continue
            candles = [candle for candle in candles if candle.close_time <= target]
            if not candles or candles[-1].close_time != target:
                # Binance may still be publishing a candle; leave its checkpoint
                # unchanged so the next poll retries instead of discarding it.
                market_failed = True
                continue
            earliest_needed = max(checkpoint if checkpoint is not None else -1,
                                  earliest_activation - 1)
            for index, candle in enumerate(candles):
                if candle.close_time <= earliest_needed:
                    continue
                signal = entry_signal(candles[:index + 1])
                if signal is not None:
                    self.store.record(symbol, candle.close_time, signal)
            # Persist all discovered signal records before declaring this symbol
            # complete. Already-created delivery keys survive a process restart.
            self.store.record(symbol, candles[-1].close_time)
            sent, failed = self._deliver(self.store.pending(symbol), attempted)
            status["alerts_sent"] += sent
            delivery_failed += failed
            status["symbols_checked"] += 1
            if status["symbols_checked"] % 25 == 0:
                status["heartbeat_ms"] = int(self.clock() * 1000)
                self.store.set_status(status)

        status["state"] = "blocked" if market_failed else "ready"
        if market_failed:
            status["error"] = "تعذر فحص بعض الأزواج؛ ستتم إعادة المحاولة تلقائياً."
        elif delivery_failed:
            status["error"] = "بعض التنبيهات تنتظر إعادة محاولة إرسالها."
        return self._finish(status)

    def run(self, stop_event):
        self._stop = stop_event
        failures = 0
        try:
            while not stop_event.is_set():
                try:
                    self.scan_once()
                except Exception as error:
                    failures += 1
                    retry_seconds = 30 if failures == 1 else 60
                    # Exception values and traceback data may contain request
                    # details. Log the type only, and keep this worker alive.
                    logger.error("Scanner cycle failed: %s", type(error).__name__)
                    try:
                        now_ms = int(self.clock() * 1000)
                        state = self.store.status()
                        state.update(state="error", last_error_ms=now_ms,
                                     heartbeat_ms=now_ms, retry_at_ms=now_ms + retry_seconds * 1000,
                                     error="تعذر إكمال دورة المراقبة؛ سأعيد المحاولة تلقائياً.")
                        self.store.set_status(state)
                    except Exception as status_error:
                        logger.error("Scanner status update failed: %s", type(status_error).__name__)
                    stop_event.wait(retry_seconds)
                else:
                    failures = 0
                    stop_event.wait(self.poll_seconds)
        finally:
            self._stop = None
