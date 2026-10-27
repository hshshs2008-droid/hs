"""Durable, opt-in delivery of closed-candle Binance spot signals."""

from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from indicators import STRATEGY_VERSION, entry_signal
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
        ("عبور RSI فوق 30: ", "عبور RSI فوق 30: ", "↗️ ", rsi_event_close_time),
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


def format_alert(symbol, signal, *, exchange_name="Binance Spot"):
    timing = "الشرطان في الشمعة نفسها."
    if signal.rsi_event_close_time != signal.stoch_event_close_time:
        timing = "الشرطان في شمعتين متتاليتين."
    message = (
        f"📈 {symbol}\n"
        f"🏷️ {exchange_name}\n"
        "⏱️ 4h • شموع مغلقة\n"
        f"إغلاق شمعة 4h: {_close_label(signal.close_time)}\n"
        f"قيم آخر إغلاق:\n"
        f"RSI (14): {signal.rsi_previous:.2f} → {signal.rsi_current:.2f}\n"
        f"K: {signal.k_previous:.2f} → {signal.k_current:.2f}\n"
        f"D: {signal.d_previous:.2f} → {signal.d_current:.2f}\n"
        f"عبور RSI فوق 30: {_close_label(signal.rsi_event_close_time)}\n"
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

    def __init__(self, path, *, exchange_name="Binance Spot"):
        if not isinstance(exchange_name, str) or not 1 <= len(exchange_name) <= 80:
            raise ValueError("Invalid exchange display name.")
        self.exchange_name = exchange_name
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._access_enforced = False
        self.admin_id = None
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
                CREATE TABLE IF NOT EXISTS access_requests (
                    chat_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'denied')),
                    requested_at_ms INTEGER NOT NULL,
                    notified INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS delivery_ledger (
                    attempt_id TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    signal_close_time INTEGER NOT NULL,
                    quote_price TEXT,
                    quote_at_ms INTEGER,
                    message_text TEXT NOT NULL,
                    attempted_at_ms INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('prepared', 'sent', 'failed', 'uncertain')),
                    telegram_message_id INTEGER,
                    telegram_sent_at_ms INTEGER,
                    confirmed_at_ms INTEGER,
                    failure_status INTEGER,
                    origin TEXT NOT NULL DEFAULT 'live'
                );
                CREATE INDEX IF NOT EXISTS delivery_ledger_recipient_signal
                    ON delivery_ledger(chat_id, symbol, signal_close_time, status);
            """)
            self._db.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES ('delivery_ledger_started_at_ms', ?)",
                (json.dumps(int(time.time() * 1000)),),
            )
            # Old messages were confirmed, but their displayed quote was never
            # saved. Retain those records with an explicitly unknown price.
            self._db.execute("""
                INSERT OR IGNORE INTO delivery_ledger(
                    attempt_id, chat_id, symbol, signal_close_time, quote_price,
                    quote_at_ms, message_text, attempted_at_ms, status,
                    telegram_sent_at_ms, confirmed_at_ms, origin)
                SELECT 'legacy:' || d.chat_id || ':' || d.symbol || ':' || d.close_time,
                    d.chat_id, d.symbol, d.close_time, NULL, NULL, s.message,
                    d.sent_at_ms, 'sent', d.sent_at_ms, d.sent_at_ms, 'legacy'
                FROM deliveries AS d JOIN signals AS s
                    ON d.symbol = s.symbol AND d.close_time = s.close_time
                WHERE d.sent_at_ms IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM delivery_ledger AS l
                    WHERE l.chat_id = d.chat_id AND l.symbol = d.symbol
                        AND l.signal_close_time = d.close_time AND l.status = 'sent')
            """)
            # Retain old signals and receipts, but don't deliver the former
            # strategy's queued alerts after a rule change. The activation
            # boundary survives restarts and applies independently per exchange.
            if self._metadata("strategy_version", None) != STRATEGY_VERSION:
                existing = self._db.execute(
                    "SELECT 1 FROM checkpoints UNION ALL SELECT 1 FROM subscriptions LIMIT 1"
                ).fetchone()
                activation = int(time.time() * 1000) if existing else 0
                self._db.executemany(
                    "INSERT INTO metadata(key,value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    [("strategy_version", json.dumps(STRATEGY_VERSION)),
                     ("strategy_activation_ms", json.dumps(activation))],
                )
            self.strategy_activation_ms = self._metadata("strategy_activation_ms", 0)

    def close(self):
        with self._lock:
            self._db.close()

    @staticmethod
    def _ledger_timestamp(value):
        if type(value) is not int or not 0 <= value < 2**63:
            raise ValueError("Delivery timestamps must be nonnegative integer milliseconds.")

    def prepare_delivery(self, chat_id, symbol, signal_close_time, quote_price,
                         quote_at_ms, message_text, attempted_at_ms):
        """Commit the exact displayed quote and payload before attempting send."""
        if type(chat_id) is not int or not 0 < chat_id < 2**52:
            raise ValueError("Delivery ledger requires a private Telegram user ID.")
        if not isinstance(symbol, str) or not 1 <= len(symbol) <= 128 or not symbol.replace("_", "").replace("-", "").replace(".", "").isalnum():
            raise ValueError("Invalid delivery symbol.")
        self._ledger_timestamp(signal_close_time)
        self._ledger_timestamp(attempted_at_ms)
        if quote_at_ms is not None:
            self._ledger_timestamp(quote_at_ms)
        if quote_price is not None:
            if not isinstance(quote_price, str) or not 1 <= len(quote_price) <= 80:
                raise ValueError("A delivery quote must be a decimal string.")
            try:
                price = Decimal(quote_price)
            except InvalidOperation:
                raise ValueError("Invalid delivery quote.") from None
            if (not price.is_finite() or price <= 0 or not -40 <= price.adjusted() <= 30
                    or quote_at_ms is None):
                raise ValueError("Invalid delivery quote or missing quote timestamp.")
        if not isinstance(message_text, str) or not 1 <= len(message_text) <= 32768:
            raise ValueError("Invalid delivery message.")
        attempt_id = uuid4().hex
        with self._lock, self._db:
            self._db.execute("""
                INSERT INTO delivery_ledger(attempt_id, chat_id, symbol, signal_close_time,
                    quote_price, quote_at_ms, message_text, attempted_at_ms, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared')
            """, (attempt_id, chat_id, symbol, signal_close_time, quote_price,
                  quote_at_ms, message_text, attempted_at_ms))
        return attempt_id

    def confirm_delivery(self, attempt_id, telegram_message_id, telegram_sent_at_ms,
                         confirmed_at_ms):
        """Commit confirmed send and pending-queue completion atomically."""
        if type(telegram_message_id) is not int or not 0 < telegram_message_id < 2**63:
            raise ValueError("Invalid Telegram message identifier.")
        self._ledger_timestamp(telegram_sent_at_ms)
        self._ledger_timestamp(confirmed_at_ms)
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT chat_id, symbol, signal_close_time, status, telegram_message_id, "
                "telegram_sent_at_ms FROM delivery_ledger WHERE attempt_id = ?", (attempt_id,),
            ).fetchone()
            if row is None or row[3] not in ('prepared', 'uncertain', 'sent'):
                raise ValueError("Delivery attempt cannot be confirmed.")
            if row[3] == 'sent':
                if (row[4], row[5]) != (telegram_message_id, telegram_sent_at_ms):
                    raise ValueError("A confirmed delivery cannot be overwritten.")
                return
            self._db.execute("""
                UPDATE delivery_ledger SET status = 'sent', telegram_message_id = ?,
                    telegram_sent_at_ms = ?, confirmed_at_ms = ?, failure_status = NULL
                WHERE attempt_id = ?
            """, (telegram_message_id, telegram_sent_at_ms, confirmed_at_ms, attempt_id))
            self._db.execute("""
                UPDATE deliveries SET sent_at_ms = ?, attempts = attempts + 1
                WHERE chat_id = ? AND symbol = ? AND close_time = ? AND sent_at_ms IS NULL
            """, (telegram_sent_at_ms, *row[:3]))

    def fail_delivery(self, attempt_id, failure_status=None, uncertain=False):
        """Keep failed or ambiguous attempts separate from confirmed history."""
        if (failure_status is not None and
                (type(failure_status) is not int or not 100 <= failure_status <= 599)):
            raise ValueError("Invalid delivery failure status.")
        if type(uncertain) is not bool:
            raise ValueError("Delivery uncertainty must be boolean.")
        with self._lock, self._db:
            self._db.execute("""
                UPDATE delivery_ledger SET status = ?, failure_status = ?
                WHERE attempt_id = ? AND status IN ('prepared', 'uncertain')
            """, ('uncertain' if uncertain else 'failed', failure_status, attempt_id))

    def report_rows(self, chat_id):
        """Return the first confirmed message for each signal for one recipient."""
        if type(chat_id) is not int or not 0 < chat_id < 2**52:
            raise ValueError("Delivery reports require a private Telegram user ID.")
        with self._lock:
            cursor = self._db.execute("""
                SELECT * FROM delivery_ledger WHERE chat_id = ? AND status = 'sent'
                ORDER BY signal_close_time, symbol,
                    COALESCE(telegram_sent_at_ms, confirmed_at_ms), attempted_at_ms, attempt_id
            """, (chat_id,))
            names = [column[0] for column in cursor.description]
            rows = cursor.fetchall()
        results = []
        seen = set()
        for values in rows:
            row = dict(zip(names, values))
            key = (row['symbol'], row['signal_close_time'])
            if key not in seen:
                seen.add(key)
                results.append(row)
        return results

    def get_ledger_started_at_ms(self):
        return self._metadata('delivery_ledger_started_at_ms', None)

    def configure_access(self, admin_id=None):
        """Enable approvals before starting workers; never claim the first visitor."""
        with self._lock:
            if admin_id is not None:
                if type(admin_id) is not int or not 0 < admin_id < 2**52:
                    raise ValueError("TELEGRAM_ADMIN_ID must be a positive Telegram user ID.")
                self._set_metadata("access_admin_id", admin_id)
            saved = self._metadata("access_admin_id", None)
            self.admin_id = saved if type(saved) is int and 0 < saved < 2**52 else None
            self._access_enforced = True
            if self.admin_id is not None:
                with self._db:
                    self._db.execute("DELETE FROM access_requests WHERE chat_id = ?", (self.admin_id,))

    def access_required(self):
        return self._access_enforced

    def is_admin(self, chat_id):
        return type(chat_id) is int and self.admin_id is not None and chat_id == self.admin_id

    def can_access(self, chat_id):
        if not self._access_enforced or self.is_admin(chat_id):
            return True
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM access_requests WHERE chat_id = ? AND status = 'approved'",
                (chat_id,),
            ).fetchone() is not None and self.admin_id is not None

    def request_access(self, chat_id, name, requested_at_ms):
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO access_requests(chat_id, name, status, requested_at_ms) "
                "VALUES (?, ?, 'pending', ?)", (chat_id, name[:160], requested_at_ms),
            )
            return self._db.execute(
                "SELECT status, notified FROM access_requests WHERE chat_id = ?", (chat_id,),
            ).fetchone()

    def mark_access_notified(self, chat_id):
        with self._lock, self._db:
            self._db.execute("UPDATE access_requests SET notified = 1 WHERE chat_id = ?", (chat_id,))

    def access_requests(self, status='pending'):
        with self._lock:
            return self._db.execute(
                "SELECT chat_id, name FROM access_requests WHERE status = ? ORDER BY requested_at_ms, chat_id",
                (status,),
            ).fetchall()

    def decide_access(self, chat_id, approve, activation_ms, *, reconsider=False):
        """Persist the decision and subscription together; reject stale buttons."""
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT status FROM access_requests WHERE chat_id = ?", (chat_id,),
            ).fetchone()
            eligible = ('pending', 'denied') if reconsider and approve else ('pending',)
            if not row or row[0] not in eligible or self.is_admin(chat_id):
                return False
            self._db.execute("UPDATE access_requests SET status = ? WHERE chat_id = ?",
                             ('approved' if approve else 'denied', chat_id))
            if approve:
                # Fresh activation prevents pre-approval pending alerts leaking.
                self._db.execute("DELETE FROM subscriptions WHERE chat_id = ?", (chat_id,))
                self._db.execute("INSERT INTO subscriptions VALUES (?, ?)", (chat_id, activation_ms))
            else:
                self._db.execute("DELETE FROM subscriptions WHERE chat_id = ?", (chat_id,))
            return True

    def revoke_access(self, chat_id):
        with self._lock, self._db:
            if self.is_admin(chat_id):
                return False
            cursor = self._db.execute(
                "UPDATE access_requests SET status = 'denied' WHERE chat_id = ? AND status = 'approved'",
                (chat_id,),
            )
            self._db.execute("DELETE FROM subscriptions WHERE chat_id = ?", (chat_id,))
            return cursor.rowcount == 1

    def subscribe(self, chat_id, activation_ms):
        with self._lock, self._db:
            if not self.can_access(chat_id):
                return False
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
            rows = self._db.execute(
                "SELECT chat_id, activation_ms FROM subscriptions ORDER BY chat_id"
            ).fetchall()
            return [(chat_id, activation) for chat_id, activation in rows if self.can_access(chat_id)]

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
                    (symbol, signal.close_time, format_alert(symbol, signal, exchange_name=self.exchange_name),
                     signal.rsi_event_close_time, signal.stoch_event_close_time),
                )
                self._db.executemany(
                    """INSERT OR IGNORE INTO deliveries(chat_id, symbol, close_time)
                       VALUES (?, ?, ?)""",
                    [(chat_id, symbol, signal.close_time) for chat_id, activation in self.subscribers()
                     if activation <= signal.close_time],
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
                   WHERE d.sent_at_ms IS NULL AND d.close_time >= ?"""
        parameters = (self.strategy_activation_ms,)
        if symbol is not None:
            query += " AND d.symbol = ?"
            parameters += (symbol,)
        query += " ORDER BY d.close_time, d.symbol, d.chat_id"
        with self._lock:
            rows = self._db.execute(query, parameters).fetchall()
        return [(chat_id, symbol, close_time,
                 _refresh_alert_times(message, close_time, rsi_time, stoch_time))
                for chat_id, symbol, close_time, message, rsi_time, stoch_time in rows
                if self.can_access(chat_id)]

    def is_pending(self, chat_id, symbol, close_time):
        with self._lock:
            if not self.can_access(chat_id):
                return False
            return self._db.execute(
                """SELECT 1 FROM deliveries WHERE chat_id = ? AND symbol = ?
                   AND close_time = ? AND sent_at_ms IS NULL AND close_time >= ?""",
                (chat_id, symbol, close_time, self.strategy_activation_ms),
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
                 request_delay=0.15, clock=time.time, sleep=time.sleep, send_signal=None):
        if poll_seconds <= 0 or request_delay < 0:
            raise ValueError("Polling must be positive and request delay nonnegative.")
        self.client = client
        self.store = store
        self.send_alert = send_alert
        self.send_signal = send_signal
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
                if self.send_signal is not None:
                    self.send_signal(chat_id, symbol, close_time, message)
                else:
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
        name = "بايننس" if self.store.exchange_name == "Binance Spot" else self.store.exchange_name
        status.update(state="blocked", blocked_until=deadline,
                      error=f"تعذر جلب بيانات {name}." +
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
            if getattr(self.client, "allow_empty_universe", False) is True:
                status["state"] = "ready"
                return self._finish(status)
            return self._block(MarketError("No active Binance spot USDT pairs."), status)
        earliest_activation = max(self.store.strategy_activation_ms,
                                  min(activation for _, activation in subscribers))
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
