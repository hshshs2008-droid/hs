"""Read-only, asynchronous reports of the approved filter's recent entries."""

from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime
from html import escape
import logging
import threading
import time
import unicodedata

from alerts import AlertDeliveryError, CLOSE_GRACE_MS, DELIVERY_SPACING_SECONDS, DISPLAY_TZ
from indicators import entry_signal
from market import FOUR_HOURS_MS, MarketError


DAY_MS = 24 * 60 * 60 * 1000
MAX_MESSAGE_CHARS = 3500
REPORT_DELIVERY_ERROR = "تعذر إرسال تقرير الإشارات كاملاً؛ أعد /signals لاحقاً."
REPORT_JOB_ERROR = "تعذر إكمال تقرير الإشارات؛ أعد /signals لاحقاً."
logger = logging.getLogger(__name__)


def historical_signals(candles, as_of_ms):
    """Return all entry completions in (as_of - 24 hours, as_of].

    Evaluate each eligible completed candle with its earlier warm-up history.
    A valid past entry remains a result even if indicators later reverse.
    """
    if isinstance(as_of_ms, bool) or not isinstance(as_of_ms, int) or as_of_ms < 0:
        raise ValueError("History requires a nonnegative integer timestamp.")
    closed = [candle for candle in candles if candle.close_time <= as_of_ms]
    cutoff = as_of_ms - DAY_MS
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


def _time_label(timestamp):
    return datetime.fromtimestamp(timestamp / 1000, DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _display_width(text):
    return sum(0 if unicodedata.combining(char) else
               2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
               for char in text)


def _pad(text, width, right=False):
    spaces = " " * max(0, width - _display_width(text))
    return spaces + text if right else text + spaces


def _table(entries, widths):
    labels = ("#", "Pair", "Time", "RSI", "K", "D")

    def border(left, middle, right):
        return left + middle.join("─" * (width + 2) for width in widths) + right

    def line(cells):
        return "│ " + " │ ".join(_pad(cell, width, right=index >= 3)
                                  for index, (cell, width) in enumerate(zip(cells, widths))) + " │"

    lines = [border("┌", "┬", "┐"), line(labels), border("├", "┼", "┤")]
    lines.extend(line(cells) for date, cells in entries)
    lines.append(border("└", "┴", "┘"))
    return "<pre>" + escape("\n".join(lines)) + "</pre>"


def _tables(entries, widths):
    blocks = []
    group = []
    current_date = None
    for date, cells in entries:
        if date != current_date and group:
            blocks.append(f"<b>📅 {current_date}</b>\n" + _table(group, widths))
            group = []
        current_date = date
        group.append((date, cells))
    if group:
        blocks.append(f"<b>📅 {current_date}</b>\n" + _table(group, widths))
    return "\n\n".join(blocks)


def _message_units(text):
    # Conservatively count markup and escaped entities as well as UTF-16
    # surrogate pairs, keeping the actual Telegram text comfortably below4096.
    return len(text.encode("utf-16-le")) // 2


def format_history(result):
    """Render escaped HTML tables, paginated without losing any entry."""
    cutoff = result.as_of_ms - DAY_MS
    matches = sorted(
        ((symbol, signal) for symbol, signal in result.matches
         if cutoff < signal.close_time <= result.as_of_ms),
        key=lambda match: (-match[1].close_time, match[0]),
    )
    header_text = (
        "إشارات تاريخية، وليست تنبيهات جديدة\n"
        "بايننس سبوت • جميع أزواج USDT • فريم 4 ساعات\n"
        f"الفترة: {_time_label(cutoff)} إلى {_time_label(result.as_of_ms)}\n"
        f"آخر إغلاق معتمد: {_time_label(result.target_close_time + 1)}\n"
        "جميع الأوقات بتوقيت إسرائيل\n"
        f"الأزواج المفحوصة: {result.symbols_checked}/{result.symbols_total}\n"
        f"عدد الإشارات: {len(matches)}\n"
    )
    if not result.complete:
        header_text += "⚠️ الفحص غير مكتمل؛ النتائج المتاحة قد لا تشمل جميع الإشارات.\n"
        if result.errors:
            errors = "، ".join(f"{reason}: {count}" for reason, count in sorted(result.errors.items()))
            header_text += f"تعذر إكمال الفحص: {errors}\n"
        if result.blocked_until:
            header_text += "يلزم انتظار مهلة بايننس ثم إعادة الطلب.\n"
    header = "<b>📊 إشارات آخر 24 ساعة</b>\n" + escape(header_text)
    if not matches:
        body = ("لم تظهر إشارات مطابقة خلال الفترة." if result.complete else
                "تعذر التأكد من وجود إشارات؛ أعد الطلب لاحقاً.")
        return [header + "\n" + escape(body)]

    rows = []
    for index, (symbol, signal) in enumerate(matches, 1):
        local = datetime.fromtimestamp((signal.close_time + 1) / 1000, DISPLAY_TZ)
        rows.append((local.strftime("%Y-%m-%d"), (
            str(index), symbol, local.strftime("%H:%M"),
            f"{signal.rsi_current:.2f}", f"{signal.k_current:.2f}", f"{signal.d_current:.2f}",
        )))
    labels = ("#", "Pair", "Time", "RSI", "K", "D")
    widths = tuple(max(_display_width(label), *(_display_width(cells[index]) for date, cells in rows))
                   for index, label in enumerate(labels))
    legend = "العملة: Pair • الوقت: Time • الستوكاست: K وD\n"
    prefix = header + escape(legend)
    # Dates, table borders/headers and markup all count toward the page limit.
    # Leave room for final page numbers before appending the next whole row.
    groups = []
    current = []
    for row in rows:
        candidate = current + [row]
        if current and _message_units(prefix + _tables(candidate, widths)) + 64 > MAX_MESSAGE_CHARS:
            groups.append(current)
            current = []
        current.append(row)
        if _message_units(prefix + _tables(current, widths)) + 64 > MAX_MESSAGE_CHARS:
            raise ValueError("A historical table row exceeds the message limit.")
    if current:
        groups.append(current)
    return [prefix + f"<b>الجزء {index}/{len(groups)}</b>\n\n" + _tables(group, widths)
            for index, group in enumerate(groups, 1)]


class HistoryService:
    """One lazily-started worker, with coalesced requests and in-memory cache."""

    def __init__(self, client, send_result, stop_event, request_delay=0.15,
                 *, clock=time.time, sleep=time.sleep):
        if request_delay < 0:
            raise ValueError("Request delay must be nonnegative.")
        self.client = client
        self.send_result = send_result
        self.stop = stop_event
        self.request_delay = request_delay
        self.clock = clock
        self.sleep = sleep
        self._condition = threading.Condition()
        self._scan_lock = threading.Lock()
        self._waiting = set()
        self._active = set()
        self._thread = None
        self._cache = None
        self._cache_key = None
        self._partial = None
        self._market_blocked_until = 0
        self._delivery_blocked_until = 0
        self._next_delivery_at = 0
        self.last_error = None
        self.last_error_ms = None
        self.heartbeat_ms = 0

    def request(self, chat_id):
        """Schedule without blocking polling; return False for a duplicate."""
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
            return self._thread is not None and self._thread.is_alive()

    def join(self, timeout=None):
        with self._condition:
            thread = self._thread
            self._condition.notify_all()
        if thread is not None:
            thread.join(timeout)

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
                                     if as_of_ms - DAY_MS < signal.close_time <= as_of_ms))

    def scan_once(self):
        """Fetch one report without subscriptions, delivery, or database writes."""
        with self._scan_lock:
            return self._scan_once()

    def _scan_once(self):
        now = int(self.clock() * 1000)
        if self.clock() < self._market_blocked_until:
            if self._partial is not None:
                return self._for_now(self._partial, now)
            return HistoryResult(now, ((now - CLOSE_GRACE_MS) // FOUR_HOURS_MS) * FOUR_HOURS_MS - 1,
                                 0, 0, (), False, {"مهلة بايننس": 1}, self._market_blocked_until)
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
                wait_until = max(self._next_delivery_at, self._delivery_blocked_until)
                if not self._pause(wait_until - self.clock()):
                    return False
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
