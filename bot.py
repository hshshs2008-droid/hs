"""Minimal Telegram bot using Python's standard library."""
import json
import logging
import math
import os
import sys
import threading
import time
from datetime import datetime
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


logger = logging.getLogger(__name__)


WELCOME = (
    "أهلاً بك! 👋\n"
    "أراقب أزواج USDT في بايننس سبوت على فريم 4 ساعات.\n"
    "RSI (14) يعبر فوق 20، والستوكاست (14، 3، 3) يتقاطع صعوداً تحت 30 "
    "بعد أن كان خطّاه تحت 20 في الشمعة السابقة.\n"
    "يكتمل الشرطان في الشمعة نفسها أو شمعتين متتاليتين.\n\n"
    "يجب أن يبقى RSI فوق 20 وK فوق D عند اكتمال الإشارة.\n\n"
    "/alerts_on — تفعيل التنبيهات في محادثة خاصة\n"
    "/alerts_off — إيقاف التنبيهات\n"
    "/status — حالة المراقبة\n"
    "/signals — جميع إشارات آخر 24 ساعة\n"
    "التنبيهات تبدأ من الشموع التي تغلق بعد التفعيل."
)


class TelegramError(Exception):
    def __init__(self, message, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def retry_seconds(result):
    try:
        value = float(result.get("parameters", {}).get("retry_after", 0))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def api(token, method, payload=None):
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=40) as response:
            result = json.load(response)
    except HTTPError as error:
        retry_after = None
        if error.code == 429:
            try:
                retry_after = retry_seconds(json.loads(error.read(65536)))
            except (OSError, ValueError, HTTPException):
                pass
        raise TelegramError(f"Telegram HTTP status: {error.code}", status=error.code,
                            retry_after=retry_after) from None
    except (URLError, TimeoutError, OSError, ValueError, HTTPException):
        raise TelegramError("تعذر الاتصال بتيليجرام. تحقق من الشبكة والتوكن.") from None
    if not isinstance(result, dict):
        raise TelegramError("استجابة تيليجرام غير صالحة.")
    if result.get("ok") is not True:
        raise TelegramError("رفض تيليجرام الطلب. تحقق من إعدادات البوت.",
                            status=result.get("error_code"), retry_after=retry_seconds(result))
    if "result" not in result:
        raise TelegramError("استجابة تيليجرام غير مكتملة.")
    return result["result"]


def handle_update(token, update, store=None, worker_alive=True, history=None):
    if not isinstance(update, dict):
        return
    message = update.get("message", {})
    if not isinstance(message, dict):
        return
    text = message.get("text", "")
    if not isinstance(text, str):
        return
    parts = text.split(maxsplit=1)
    command = parts[0].split("@", 1)[0] if parts else ""
    chat = message.get("chat")
    if command in ("/start", "/alerts_on", "/alerts_off", "/status", "/signals"):
        if not isinstance(chat, dict) or not isinstance(chat.get("id"), int):
            return
    if command == "/start":
        api(token, "sendMessage", {"chat_id": message["chat"]["id"], "text": WELCOME})
    elif command in ("/alerts_on", "/alerts_off", "/status", "/signals") and store is not None:
        chat = message["chat"]
        chat_id = chat["id"]
        if chat.get("type") != "private":
            reply = "افتح محادثة خاصة مع البوت لتفعيل التنبيهات أو إدارتها."
        elif command == "/signals":
            if history is None:
                reply = "عرض الإشارات غير متاح الآن؛ حاول لاحقاً."
            else:
                # Acknowledge first: a failed acknowledgement must not create
                # duplicate background jobs when the polling loop retries.
                api(token, "sendMessage", {"chat_id": chat_id, "text": (
                    "جارٍ تجهيز جميع إشارات آخر 24 ساعة على فريم 4 ساعات.\n"
                    "قد يستغرق الفحص الأول بضع دقائق؛ ستصل القائمة هنا.\n"
                    "هذه إشارات تاريخية وليست تنبيهات جديدة."
                )})
                history.request(chat_id)
                return
        elif command == "/alerts_on":
            store.subscribe(chat_id, int(time.time() * 1000))
            reply = (
                "تم تفعيل التنبيهات ✅\n"
                "سأرسل تنبيهاً عند اكتمال الشروط على شموع 4 ساعات المغلقة بعد التفعيل.\n"
                "استخدم /status للتحقق من وصول بيانات بايننس."
            )
        elif command == "/alerts_off":
            store.unsubscribe(chat_id)
            reply = "تم إيقاف التنبيهات. لتفعيلها مجدداً أرسل /alerts_on."
        else:
            active = any(cid == chat_id for cid, _ in store.subscribers())
            state = store.status()
            labels = {
                "idle": "المراقبة تنتظر تفعيل التنبيهات.",
                "ready": "آخر دورة مراقبة مكتملة.",
                "scanning": "جارٍ فحص العملات.",
                "blocked": "تعذر جلب بيانات بايننس. سأعيد المحاولة تلقائياً.",
                "backoff": "المراقبة تنتظر انتهاء مهلة بايننس قبل إعادة المحاولة.",
                "error": "تعذر إكمال المراقبة؛ يلزم فحص تشغيل البوت.",
            }
            status_text = labels.get(state.get("state"), "المراقبة قيد التهيئة.")
            if not worker_alive:
                status_text = "توقفت المراقبة؛ يلزم إعادة تشغيل البوت وفحصه."
            reply = ("تنبيهاتك: " + ("مفعّلة" if active else "متوقفة") + "\n" + status_text)
            if state.get("symbols_checked") is not None:
                reply += f"\nالأزواج المفحوصة في آخر دورة: {state['symbols_checked']}"
            last_scan = state.get("last_scan_ms")
            if isinstance(last_scan, (int, float)) and not isinstance(last_scan, bool):
                from alerts import DISPLAY_TZ
                label = datetime.fromtimestamp(last_scan / 1000, DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S")
                reply += f"\nآخر دورة فحص: {label} (بتوقيت إسرائيل)"
            if state.get("pending", 0):
                reply += "\nتوجد تنبيهات معلّقة تنتظر إعادة محاولة الإرسال."
            if history is not None and history.last_error:
                reply += "\n" + history.last_error
        api(token, "sendMessage", {"chat_id": chat_id, "text": reply})


def process_updates(token, updates, store, worker_alive=True, history=None):
    """Acknowledge permanent reply failures without blocking later commands.

    Transient failures leave the current update unacknowledged for retry.
    Never log chat IDs, message contents, tokens or raw server exceptions.
    """
    if not isinstance(updates, list):
        raise TelegramError("قائمة تحديثات تيليجرام غير صالحة.")
    offset = store.get_update_offset()
    for update in updates:
        update_id = update.get("update_id") if isinstance(update, dict) else None
        if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 0:
            logger.warning("Ignored malformed Telegram update without a valid identifier")
            continue
        if update_id < offset:
            continue
        try:
            handle_update(token, update, store, worker_alive, history)
        except TelegramError as error:
            permanent = (isinstance(error.status, int) and 400 <= error.status < 500
                         and error.status not in (408, 409, 429))
            if not permanent:
                raise
            logger.warning("Skipped permanently rejected command reply; HTTP status=%s", error.status)
        offset = update_id + 1
        store.set_update_offset(offset)
    return offset


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("أضف TELEGRAM_BOT_TOKEN بأمان في إعدادات البيئة.", file=sys.stderr)
        return 1
    try:
        api(token, "getMe")
        webhook = api(token, "getWebhookInfo")
        if webhook.get("url"):
            print("يوجد webhook نشط. أوقفه قبل تشغيل polling.", file=sys.stderr)
            return 1
        print("Telegram authentication and polling configuration verified.", flush=True)
        from alerts import AlertDeliveryError, AlertStore, Scanner
        from market import BinanceClient, MarketError
        from indicators import entry_signal
        from history import HistoryService

        client = BinanceClient()
        if "--check" in sys.argv:
            try:
                server_time = client.server_time()
                symbols = client.spot_symbols()
                if not symbols:
                    print("Binance returned no active spot USDT symbols.", file=sys.stderr)
                    return 1
                sample = "BTCUSDT" if "BTCUSDT" in symbols else symbols[0]
                candles = client.candles(sample, server_time)
                if len(candles) < 100:
                    print("Insufficient candle history for market validation.", file=sys.stderr)
                    return 1
                entry_signal(candles)
                print(f"Binance market data verified: {len(symbols)} spot USDT pairs, {len(candles)} closed 4h candles.")
            except MarketError as error:
                print(str(error), file=sys.stderr)
                return 1
            return 0
        state_path = os.environ.get("ALERT_STATE_PATH") or os.path.join(os.path.dirname(__file__), ".state", "alerts.sqlite3")
        store = AlertStore(state_path)
        stop = threading.Event()
        delivery_lock = threading.Lock()
        next_message_at = 0.0

        def send_alert(chat_id, text, parse_mode=None):
            nonlocal next_message_at
            # Market alerts and historical pages share one paced sender.
            with delivery_lock:
                remaining = store.get_delivery_blocked_until() - time.time()
                if remaining > 0:
                    raise AlertDeliveryError("إرسال الرسائل ينتظر مهلة تيليجرام.",
                                             status=429, retry_after=remaining)
                delay = max(0, next_message_at - time.monotonic())
                if stop.wait(delay):
                    raise AlertDeliveryError("توقف إرسال الرسائل.")
                next_message_at = time.monotonic() + 1.05
                try:
                    payload = {"chat_id": chat_id, "text": text}
                    if parse_mode is not None:
                        payload["parse_mode"] = parse_mode
                    api(token, "sendMessage", payload)
                except TelegramError as error:
                    if error.status == 429:
                        store.set_delivery_blocked_until(time.time() + max(60, error.retry_after or 0))
                    raise AlertDeliveryError(str(error), status=error.status,
                                             retry_after=error.retry_after) from None

        scanner = Scanner(client, store, send_alert)
        history = HistoryService(client, lambda chat_id, text: send_alert(chat_id, text, "HTML"), stop)
        try:
            api(token, "setMyCommands", {"commands": [
                {"command": "start", "description": "شرح البوت والفلتر"},
                {"command": "signals", "description": "عرض جميع إشارات آخر 24 ساعة"},
                {"command": "alerts_on", "description": "تفعيل التنبيهات"},
                {"command": "alerts_off", "description": "إيقاف التنبيهات"},
                {"command": "status", "description": "حالة المراقبة والتنبيهات"},
            ]})
        except TelegramError as error:
            logger.warning("Command menu refresh failed; HTTP status=%s", error.status)

        def monitor():
            while not stop.is_set():
                try:
                    scanner.run(stop)
                except Exception as error:
                    logger.error("Scanner worker will restart; exception type=%s", type(error).__name__)
                if not stop.is_set():
                    stop.wait(30)

        worker = threading.Thread(target=monitor, name="market-scanner", daemon=True)
        worker.start()
        print("البوت يعمل وينتظر الرسائل.", flush=True)
        offset = store.get_update_offset()
        next_health_log = 0.0
        try:
            while True:
                try:
                    updates = api(token, "getUpdates", {"offset": offset, "timeout": 30, "allowed_updates": ["message"]})
                    offset = process_updates(token, updates, store, worker.is_alive(), history)
                    if updates:
                        logger.info("Telegram update batch handled; count=%s", len(updates))
                except TelegramError as error:
                    # Earlier updates in a failed batch may already be saved.
                    offset = store.get_update_offset()
                    logger.warning("Telegram polling/reply retry; HTTP status=%s", error.status)
                    if stop.wait(max(5, error.retry_after or 0)):
                        break
                if time.monotonic() >= next_health_log:
                    state = store.status()
                    logger.info("health worker_alive=%s state=%s checked=%s/%s pending=%s last_scan_ms=%s",
                                worker.is_alive(), state.get("state"), state.get("symbols_checked", 0),
                                state.get("symbols_total", 0), state.get("pending", 0), state.get("last_scan_ms"))
                    next_health_log = time.monotonic() + 60
        finally:
            stop.set()
            worker.join(timeout=25)
            history.join(timeout=25)
            if not worker.is_alive() and not history.is_alive():
                store.close()
    except TelegramError as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        logger.error("Bot process must restart; exception type=%s", type(error).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
