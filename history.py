"""Read-only, asynchronous reports of the approved filter's recent entries."""

from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from html import escape
import logging
import threading
import time

from alerts import AlertDeliveryError, CLOSE_GRACE_MS, DELIVERY_SPACING_SECONDS, DISPLAY_TZ
from indicators import STRATEGY_VERSION, Signal, entry_signal
from market import FOUR_HOURS_MS, MarketError


HISTORY_WINDOW_MS = 48 * 60 * 60 * 1000
MAX_MESSAGE_CHARS = 3500
REPORT_DELIVERY_ERROR = "تعذر إرسال تقرير الإشارات كاملاً؛ أعد /signals لاحقاً."
REPORT_JOB_ERROR = "تعذر إكمال تقرير الإشارات؛ أعد /signals لاحقاً."
logger = logging.getLogger(__name__)


def historical_signals(candles, as_of_ms):
    """Return all entry completions in (as_of - 48 hours, as_of].

    Evaluate each eligible completed candle with its earlier warm-up history.
    A valid past entry remains a result even if indicators later reverse.
    """
    if isinstance(as_of_ms, bool) or not isinstance(as_of_ms, int) or as_of_ms < 0:
        raise ValueError("History requires a nonnegative integer timestamp.")
    closed = [candle for candle in candles if candle.close_time <= as_of_ms]
    cutoff = as_of_ms - HISTORY_WINDOW_MS
    results = []
    for index, candle in enumerate(closed):
        if candle.close_time > cutoff:
            signal = entry_signal(closed[:index + 1])
            if signal is not None:
                results.append(signal)
    return results


@dataclass(frozen=True)
class HistoryResult:
    as_of_ms: int
    target_close_time: int
    symbols_checked: int
    symbols_total: int
    matches: tuple
    complete: bool
    errors: dict
    blocked_until: float = 0
    cached_at_ms: int | None = None
    refreshing: bool = False
    exchange_name: str = "Binance Spot"


def _time_label(timestamp):
    return datetime.fromtimestamp(timestamp / 1000, DISPLAY_TZ).strftime("%Y-%m-%d (%H:%M:%S)")


def _signal_row(index, symbol, signal):
    local = datetime.fromtimestamp((signal.close_time + 1) / 1000, DISPLAY_TZ)
    label = (symbol[:-5] + "/USDT" if symbol.endswith("_USDT") else
             symbol[:-4] + "/USDT" if symbol.endswith("USDT") else symbol)
    coin = f"<code>{escape(label)}</code>"
    return (f'<code>{index:02d}.</code> {coin} ✅ '
            f'<code>{local:%Y-%m-%d} ({local:%H:%M})</code>')


def _message_units(text):
    # Conservatively count markup and escaped entities as well as UTF-16
    # surrogate pairs, keeping the actual Telegram text comfortably below4096.
    return len(text.encode("utf-16-le")) // 2


def format_history(result):
    """Render one copyable coin per line, paginated without losing any entry."""
    cutoff = result.as_of_ms - HISTORY_WINDOW_MS
    matches = sorted(
        ((symbol, signal) for symbol, signal in result.matches
         if cutoff < signal.close_time <= result.as_of_ms),
        key=lambda match: (-match[1].close_time, match[0]),
    )
    header_text = (
        "إشارات تاريخية، وليست تنبيهات جديدة\n"
        f"{result.exchange_name} • "
        + "أزواج USDT المختارة"
        + " • 4h\n"
        f"الفترة: {_time_label(cutoff)} إلى {_time_label(result.as_of_ms)}\n"
        f"آخر إغلاق معتمد: {_time_label(result.target_close_time + 1)}\n"
        f"الأزواج المفحوصة: {result.symbols_checked}/{result.symbols_total}\n"
        f"عدد الإشارات: {len(matches)}\n"
    )
    if result.cached_at_ms is not None:
        header_text += f"💾 آخر تحديث للتقرير: {_time_label(result.cached_at_ms)}\n"
    if result.refreshing:
        header_text += "⏳ التحديث في الخلفية؛ المعروض هو آخر تقرير متاح.\n"
    if not result.complete:
        header_text += "⚠️ الفحص غير مكتمل؛ النتائج المتاحة قد لا تشمل جميع الإشارات.\n"
        if result.errors:
            errors = "، ".join(f"{reason}: {count}" for reason, count in sorted(result.errors.items()))
            header_text += f"تعذر إكمال الفحص: {errors}\n"
        if result.blocked_until:
            name = "بايننس" if result.exchange_name == "Binance Spot" else result.exchange_name
            header_text += f"يلزم انتظار مهلة {name} ثم إعادة الطلب.\n"
    header = "<b>📊 إشارات آخر 48 ساعة</b>\n" + escape(header_text)
    if not matches:
        body = ("لم تظهر إشارات مطابقة خلال الفترة." if result.complete else
                "تعذر التأكد من وجود إشارات؛ أعد الطلب لاحقاً.")
        return [header + "\n" + escape(body)]

    rows = [_signal_row(index, symbol, signal)
            for index, (symbol, signal) in enumerate(matches, 1)]
    groups = []
    current = []
    for row in rows:
        candidate = current + [row]
        if current and _message_units(header + "\n".join(candidate)) + 64 > MAX_MESSAGE_CHARS:
            groups.append(current)
            current = []
        current.append(row)
        if _message_units(header + "\n".join(current)) + 64 > MAX_MESSAGE_CHARS:
            raise ValueError("A historical signal row exceeds the message limit.")
    if current:
        groups.append(current)
    return [header + f"<b>الجزء {index}/{len(groups)}</b>\n\n" + "\n".join(group)
            for index, group in enumerate(groups, 1)]


class HistoryService:
    """Background refresh with durable snapshots and coalesced report delivery."""

    def __init__(self, client, send_result, stop_event, request_delay=0.15,
                 *, clock=time.time, sleep=time.sleep, store=None, exchange_name=None):
        if request_delay < 0:
            raise ValueError("Request delay must be nonnegative.")
        self.client = client
        self.send_result = send_result
        self.stop = stop_event
        self.request_delay = request_delay
        self.clock = clock
        self.sleep = sleep
        self.store = store
        name = exchange_name if exchange_name is not None else getattr(store, "exchange_name", "Binance Spot")
        self.exchange_name = name if isinstance(name, str) else "Binance Spot"
        if not 1 <= len(self.exchange_name) <= 80:
            raise ValueError("Invalid history exchange name.")
        self._condition = threading.Condition()
        self._scan_lock = threading.Lock()
        self._waiting = set()
        self._active = set()
        self._thread = None
        self._background = None
        self._cache = None
        self._cache_key = None
        self._partial = None
        self._market_blocked_until = 0
        self._delivery_blocked_until = 0
        self._next_delivery_at = 0
        self.last_error = None
        self.refresh_error = None
        self.last_error_ms = None
        self.heartbeat_ms = 0
        self._saved_complete = None
        self._saved_partial = None
        self._restore_cache()

    def _restore_cache(self):
        if self.store is None:
            return
        try:
            reports = self.store.get_history_cache()
            if not isinstance(reports, dict):
                return
            for kind in ("complete", "partial"):
                try:
                    payload = reports.get(kind)
                    if (not payload or payload.get("version") != 2
                            or payload.get("strategy_version") != STRATEGY_VERSION
                            or payload.get("window_ms") != HISTORY_WINDOW_MS):
                        continue
                    data = dict(payload["result"])
                    data["matches"] = tuple((symbol, Signal(**signal))
                                             for symbol, signal in data["matches"])
                    result = HistoryResult(**data)
                    if (not isinstance(result.as_of_ms, int) or isinstance(result.as_of_ms, bool)
                            or not 0 < result.as_of_ms <= int(self.clock() * 1000) + FOUR_HOURS_MS
                            or not isinstance(result.target_close_time, int)
                            or not 0 < result.target_close_time < result.as_of_ms
                            or (result.target_close_time + 1) % FOUR_HOURS_MS
                            or result.complete is not (kind == "complete")
                            or not isinstance(result.errors, dict)
                            or result.exchange_name != self.exchange_name):
                        raise ValueError("Invalid cached report.")
                    for symbol, signal in result.matches:
                        if (not isinstance(symbol, str) or not symbol.replace("_", "").replace("-", "").replace(".", "").isalnum() or len(symbol) > 128
                                or not isinstance(signal.close_time, int) or signal.close_time < 0):
                            raise ValueError("Invalid cached signal.")
                    if kind == "complete":
                        key = payload.get("cache_key")
                        if key is not None:
                            if (len(key) != 2 or key[0] != result.target_close_time
                                    or not isinstance(key[1], (list, tuple))
                                    or not all(isinstance(symbol, str) for symbol in key[1])):
                                raise ValueError("Invalid cached universe.")
                            self._cache_key = (key[0], tuple(key[1]))
                        self._cache = result
                        self._saved_complete = result
                    else:
                        self._partial = result
                        self._saved_partial = result
                except (TypeError, ValueError, KeyError, AttributeError, IndexError):
                    logger.warning("Ignoring invalid saved history report.")
        except Exception as error:
            logger.warning("Cannot read saved history; error_type=%s", type(error).__name__)

    def _persist_cache(self):
        if self.store is None:
            return
        for kind, result, saved in (("complete", self._cache, "_saved_complete"),
                                    ("partial", self._partial, "_saved_partial")):
            if result is not None and result is not getattr(self, saved):
                self.store.save_history_cache(kind, {
                    "version": 2, "strategy_version": STRATEGY_VERSION,
                    "window_ms": HISTORY_WINDOW_MS, "result": asdict(result),
                    "cache_key": self._cache_key if kind == "complete" else None,
                })
                setattr(self, saved, result)

    def cached_result(self):
        """Return a snapshot immediately, including while a refresh is busy."""
        result = self._cache if self._cache is not None else self._partial
        if result is None:
            return None
        now = int(self.clock() * 1000)
        target = ((now - CLOSE_GRACE_MS) // FOUR_HOURS_MS) * FOUR_HOURS_MS - 1
        current = result.target_close_time >= target
        return replace(self._for_now(result, now), cached_at_ms=result.as_of_ms,
                       complete=result.complete and current,
                       refreshing=not current or not result.complete)

    def start_background(self):
        with self._condition:
            if self.stop.is_set() or (self._background is not None and self._background.is_alive()):
                return False
            self._background = threading.Thread(target=self._run_background,
                                                name="history-refresh", daemon=True)
            self._background.start()
            return True

    def _run_background(self):
        while not self.stop.is_set():
            delay = 30
            try:
                snapshot = self.cached_result()
                if snapshot is not None and snapshot.complete:
                    self._persist_cache()
                    self.refresh_error = None
                    next_close = snapshot.target_close_time + 1 + FOUR_HOURS_MS + CLOSE_GRACE_MS
                    delay = max(1, min(60, next_close / 1000 - self.clock()))
                else:
                    result = self.scan_once()
                    if result.complete:
                        self.refresh_error = None
                        delay = 0
                    else:
                        self.refresh_error = "تعذر إكمال تحديث تقرير الإشارات؛ ستتم إعادة المحاولة تلقائياً."
                        delay = max(30, min(60, result.blocked_until - self.clock()))
                self.heartbeat_ms = int(self.clock() * 1000)
            except Exception as error:
                self.last_error_ms = int(self.clock() * 1000)
                self.refresh_error = "تعذر تحديث تقرير الإشارات؛ ستتم إعادة المحاولة تلقائياً."
                logger.error("Background history refresh will retry; error_type=%s", type(error).__name__)
            self.stop.wait(delay)

    def request(self, chat_id):
        """Schedule without blocking polling; return False for a duplicate."""
        if self.store is not None and not self.store.can_access(chat_id):
            return False
        with self._condition:
            if self.stop.is_set() or chat_id in self._waiting or chat_id in self._active:
                return False
            self._waiting.add(chat_id)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="history-reports", daemon=True)
                self._thread.start()
            self._condition.notify_all()
            return True

    def is_alive(self):
        with self._condition:
            return any(thread is not None and thread.is_alive()
                       for thread in (self._thread, self._background))

    def join(self, timeout=None):
        with self._condition:
            threads = (self._thread, self._background)
            self._condition.notify_all()
        deadline = None if timeout is None else time.monotonic() + timeout
        for thread in threads:
            if thread is not None:
                remaining = None if deadline is None else max(0, deadline - time.monotonic())
                thread.join(remaining)

    def _pause(self, delay):
        # Short waits make both long Retry-After cooldowns and shutdown
        # interruptible. An injected sleeper supports deterministic tests.
        remaining = max(delay, 0)
        while remaining > 0 and not self.stop.is_set():
            part = min(remaining, 0.25)
            if self.sleep is time.sleep:
                self.stop.wait(part)
            else:
                self.sleep(part)
            remaining -= part
        return not self.stop.is_set()

    def _request(self, method, *args):
        if not self._pause(self.request_delay):
            raise MarketError("History scan stopped.")
        return method(*args)

    def _blocked_result(self, error, as_of_ms, target, checked, total, matches, errors):
        reason = f"HTTP {error.status}" if error.status is not None else "بيانات غير متاحة"
        errors[reason] += 1
        if error.status in (418, 429, 403, 451):
            deadline = getattr(error, "cooldown_until", None)
            if deadline is None:
                deadline = self.clock() + max(300 if error.status == 418 else 60, error.retry_after or 0)
            self._market_blocked_until = deadline
        result = HistoryResult(as_of_ms, target, checked, total, tuple(matches), False,
                               dict(errors), self._market_blocked_until)
        self._partial = result
        return result

    def _for_now(self, result, as_of_ms):
        return replace(result, as_of_ms=as_of_ms,
                       matches=tuple((symbol, signal) for symbol, signal in result.matches
                                     if as_of_ms - HISTORY_WINDOW_MS < signal.close_time <= as_of_ms))

    def scan_once(self):
        """Refresh the report cache without changing live alert records."""
        with self._scan_lock:
            # A cold request may have waited behind the initial background
            # scan. Use its newly available snapshot rather than scan twice.
            if self._background is not None and threading.current_thread() is not self._background:
                snapshot = self.cached_result()
                if snapshot is not None:
                    return snapshot
            original = self._scan_once()
            result = replace(original, exchange_name=self.exchange_name)
            if self._cache is original:
                self._cache = result
            if self._partial is original:
                self._partial = result
            self._persist_cache()
            return result

    def _scan_once(self):
        now = int(self.clock() * 1000)
        if self.clock() < self._market_blocked_until:
            if self._partial is not None:
                return self._for_now(self._partial, now)
            return HistoryResult(now, ((now - CLOSE_GRACE_MS) // FOUR_HOURS_MS) * FOUR_HOURS_MS - 1,
                                 0, 0, (), False, {"مهلة " + ("بايننس" if self.exchange_name == "Binance Spot" else self.exchange_name): 1}, self._market_blocked_until)
        errors = Counter()
        target = ((now - CLOSE_GRACE_MS) // FOUR_HOURS_MS) * FOUR_HOURS_MS - 1
        try:
            as_of = self._request(self.client.server_time)
            target = ((as_of - CLOSE_GRACE_MS) // FOUR_HOURS_MS) * FOUR_HOURS_MS - 1
            symbols = self._request(self.client.spot_symbols)
        except MarketError as error:
            return self._blocked_result(error, now, target, 0, 0, [], errors)
        cache_key = (target, tuple(symbols))
        if self._cache is not None and self._cache_key == cache_key:
            return self._for_now(self._cache, as_of)
        if not symbols:
            if getattr(self.client, "allow_empty_universe", False) is True:
                result = HistoryResult(as_of, target, 0, 0, (), True, {})
                self._cache, self._cache_key = result, cache_key
                return result
            return self._blocked_result(MarketError("No active symbols."), as_of, target, 0, 0, [], errors)
        matches = []
        checked = 0
        for symbol in symbols:
            if self.stop.is_set():
                errors["توقف الفحص"] += 1
                break
            try:
                candles = self._request(self.client.candles, symbol, as_of, 500)
                closed = [candle for candle in candles if candle.close_time <= target]
                if not closed or closed[-1].close_time != target:
                    raise MarketError("Latest confirmed closed candle is unavailable.")
            except MarketError as error:
                if error.status in (418, 429, 403, 451):
                    return self._blocked_result(error, as_of, target, checked, len(symbols), matches, errors)
                errors[f"HTTP {error.status}" if error.status is not None else "بيانات غير متاحة"] += 1
                continue
            matches.extend((symbol, signal) for signal in historical_signals(closed, as_of))
            checked += 1
            self.heartbeat_ms = int(self.clock() * 1000)
        complete = checked == len(symbols) and not errors
        result = HistoryResult(as_of, target, checked, len(symbols), tuple(matches), complete, dict(errors))
        if complete:
            self._cache = result
            self._cache_key = cache_key
        else:
            self._partial = result
        return result

    def _send_pages(self, chat_id, pages):
        for page in pages:
            while not self.stop.is_set():
                if self.store is not None and not self.store.can_access(chat_id):
                    return True  # Revoked requests are cancelled, not delivered.
                wait_until = max(self._next_delivery_at, self._delivery_blocked_until)
                if not self._pause(wait_until - self.clock()):
                    return False
                if self.store is not None and not self.store.can_access(chat_id):
                    return True
                self._next_delivery_at = self.clock() + DELIVERY_SPACING_SECONDS
                self.heartbeat_ms = int(self.clock() * 1000)
                try:
                    self.send_result(chat_id, page)
                except AlertDeliveryError as error:
                    if error.status == 429:
                        self._delivery_blocked_until = self.clock() + max(60, error.retry_after or 0)
                        continue
                    self.last_error = REPORT_DELIVERY_ERROR
                    self.last_error_ms = int(self.clock() * 1000)
                    logger.warning("History result delivery failed: %s", type(error).__name__)
                    return False
                else:
                    break
            else:
                return False
        return True

    def _run(self):
        try:
            while not self.stop.is_set():
                with self._condition:
                    while not self._waiting and not self.stop.is_set():
                        self._condition.wait(timeout=0.25)
                    if self.stop.is_set():
                        break
                    recipients = set(self._waiting)
                    self._waiting.clear()
                    self._active.update(recipients)
                failed = False
                self.heartbeat_ms = int(self.clock() * 1000)
                try:
                    result = self.cached_result()
                    if result is None:
                        result = self.scan_once()
                    with self._condition:
                        # Chats requesting during the cold scan share its result.
                        recipients.update(self._waiting)
                        self._waiting.clear()
                        self._active.update(recipients)
                    pages = format_history(result)
                    all_delivered = True
                    for chat_id in sorted(recipients):
                        if self.stop.is_set():
                            all_delivered = False
                            break
                        if not self._send_pages(chat_id, pages):
                            all_delivered = False
                        with self._condition:
                            self._active.discard(chat_id)
                    if not self.stop.is_set():
                        # One accepted page does not mean the whole report arrived.
                        self.last_error = None if all_delivered else REPORT_DELIVERY_ERROR
                except Exception as error:
                    failed = True
                    self.last_error = REPORT_JOB_ERROR
                    self.last_error_ms = int(self.clock() * 1000)
                    logger.error("History report job failed: %s", type(error).__name__)
                    for chat_id in sorted(recipients):
                        if self.stop.is_set():
                            break
                        try:
                            self._send_pages(chat_id, [REPORT_JOB_ERROR])
                        except Exception as delivery_error:
                            logger.error("History failure report delivery failed: %s",
                                         type(delivery_error).__name__)
                    self.last_error = REPORT_JOB_ERROR
                finally:
                    with self._condition:
                        self._active.difference_update(recipients)
                        self._condition.notify_all()
                if failed:
                    # Keep requests queued during the failed job. Do not retry
                    # that same broken job forever or hammer a failing service.
                    self._pause(30)
        finally:
            with self._condition:
                self._active.clear()
                if self.stop.is_set():
                    self._waiting.clear()
                self._condition.notify_all()
