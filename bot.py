"""Minimal Telegram bot using Python's standard library."""
import json
import logging
import math
import os
import sys
import threading
import time
import uuid
from datetime import datetime
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from market import MarketError


logger = logging.getLogger(__name__)


WELCOME = (
    "أهلاً بك! 👋\n"
    "أراقب أزواج USDT في Binance Spot على فريم 4h.\n"
    "RSI (14) يعبر فوق 20، والستوكاست (14، 3، 3) يتقاطع صعوداً تحت 30 "
    "بعد أن كان خطّاه تحت 20 في الشمعة السابقة.\n"
    "يكتمل الشرطان في الشمعة نفسها أو شمعتين متتاليتين.\n\n"
    "يجب أن يبقى RSI فوق 20 وK فوق D عند اكتمال الإشارة.\n\n"
    "/alerts_on — تفعيل التنبيهات في محادثة خاصة\n"
    "/alerts_off — إيقاف التنبيهات\n"
    "/status — حالة المراقبة\n"
    "/signals — جميع إشارات آخر 48 ساعة\n"
    "/myid — رقم حسابك في تيليجرام\n"
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
    return _telegram_request(request)


def _telegram_request(request):
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


def send_document(token, chat_id, content, filename, caption=""):
    """Upload an in-memory Excel file; never put credentials into errors."""
    if (not isinstance(content, bytes) or not content or len(content) > 45 * 1024 * 1024
            or not isinstance(filename, str) or not 1 <= len(filename) <= 100
            or not all(char.isascii() and (char.isalnum() or char in "._-") for char in filename)):
        raise ValueError("Invalid report document.")
    boundary = "SpotReport" + uuid.uuid4().hex
    chunks = []
    for name, value in (("chat_id", str(chat_id)), ("caption", caption)):
        chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                       f'{value}\r\n').encode("utf-8"))
    chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
                   f'filename="{filename}"\r\nContent-Type: '
                   'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n').encode("ascii"))
    chunks.extend((content, f"\r\n--{boundary}--\r\n".encode("ascii")))
    request = Request(f"https://api.telegram.org/bot{token}/sendDocument", data=b"".join(chunks),
                      headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    return _telegram_request(request)


def send_alert_message(token, client, chat_id, text, parse_mode=None, *, before_send=None,
                       store=None, signal_close_time=None):
    """Capture the exact quoted payload before send; confirm only Telegram receipts."""
    price = quote_at_ms = symbol = None
    if parse_mode is None and text.startswith("📈 "):
        heading, separator, body = text.partition("\n")
        candidate = heading.removeprefix("📈 ").strip()
        if candidate.replace("_", "").replace("-", "").replace(".", "").isalnum() and candidate.endswith("USDT"):
            symbol = candidate
            try:
                price = client.latest_price(symbol)
                quote_at_ms = int(time.time() * 1000)
                whole, dot, fraction = price.partition(".")
                display = f"{int(whole):,}" + (dot + fraction if fraction else "")
                quote = f"💵 {display} USDT (عند الإرسال)"
            except MarketError as error:
                logger.warning("Latest price unavailable; HTTP status=%s", error.status)
                quote = "💵 السعر غير متاح"
            text = f"{heading} | {quote}{separator}{body}"
    if store is not None and (symbol is None or signal_close_time is None or parse_mode is not None):
        raise ValueError("Durable live delivery requires explicit signal identity.")
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
        payload["link_preview_options"] = {"is_disabled": True}
    if before_send is not None:
        before_send()
    attempt_id = None
    if store is not None:
        attempt_id = store.prepare_delivery(chat_id, symbol, signal_close_time, price, quote_at_ms,
                                            text, int(time.time() * 1000))
    try:
        result = api(token, "sendMessage", payload)
    except TelegramError as error:
        if attempt_id is not None:
            status = error.status if type(error.status) is int and 100 <= error.status <= 599 else None
            uncertain = status is None or status == 408 or status >= 500
            store.fail_delivery(attempt_id, status, uncertain=uncertain)
        raise
    if attempt_id is not None:
        valid = (isinstance(result, dict) and type(result.get("message_id")) is int
                 and result["message_id"] > 0 and type(result.get("date")) is int and result["date"] >= 0
                 and isinstance(result.get("chat"), dict) and type(result["chat"].get("id")) is int
                 and result["chat"]["id"] == chat_id)
        if not valid:
            store.fail_delivery(attempt_id, uncertain=True)
            raise TelegramError("تعذر تأكيد بيانات الإرسال؛ ستتم إعادة المحاولة.")
        store.confirm_delivery(attempt_id, result["message_id"], result["date"] * 1000,
                               int(time.time() * 1000))
    return result


def ensure_signal_access(store, chat_id):
    from alerts import AlertDeliveryError
    if not store.can_access(chat_id):
        raise AlertDeliveryError("قراءة الإشارات تتطلب موافقة المدير.", status=403)


def access_buttons(chat_id, member=False, serial=None):
    suffix = f" {serial}" if serial is not None else ""
    buttons = ([{"text": "سحب الصلاحية" + suffix, "callback_data": f"revoke:{chat_id}"}] if member else [
        {"text": "موافقة" + suffix + " ✅", "callback_data": f"approve:{chat_id}"},
        {"text": "رفض" + suffix + " ❌", "callback_data": f"deny:{chat_id}"},
    ])
    return {"inline_keyboard": [buttons]}


def notify_access_request(token, store, chat_id, name):
    if store.admin_id is None:
        return
    try:
        api(token, "sendMessage", {"chat_id": store.admin_id,
            "text": f"طلب انضمام جديد\n{name}\nرقم الحساب: {chat_id}",
            "reply_markup": access_buttons(chat_id)})
        store.mark_access_notified(chat_id)
    except TelegramError as error:
        logger.warning("Access request notification deferred; HTTP status=%s", error.status)


def handle_access_callback(token, callback, store):
    if not isinstance(callback, dict) or not isinstance(callback.get("id"), str):
        return
    message = callback.get("message", {})
    chat = message.get("chat", {}) if isinstance(message, dict) else {}
    sender = callback.get("from", {})
    sender_id = sender.get("id") if isinstance(sender, dict) else None
    allowed = (isinstance(chat, dict) and chat.get("type") == "private"
               and store.is_admin(sender_id) and chat.get("id") == sender_id)
    data = callback.get("data", "")
    action, separator, target = data.partition(":") if isinstance(data, str) else ("", "", "")
    if not allowed:
        reply = "هذا الإجراء متاح للمدير فقط."
    elif not separator or action not in ("approve", "deny", "revoke") or len(target) > 16 or not target.isascii() or not target.isdecimal() or not 0 < int(target) < 2**52:
        reply = "طلب غير صالح."
    else:
        chat_id = int(target)
        if action == "revoke":
            changed = store.revoke_access(chat_id)
            reply = "تم سحب الصلاحية وإيقاف التنبيهات." if changed else "الصلاحية غير مفعّلة لهذا الحساب."
            notice = "تم إلغاء صلاحيتك لقراءة الإشارات وإيقاف تنبيهاتك."
        else:
            changed = store.decide_access(chat_id, action == "approve", int(time.time() * 1000))
            reply = ("تمت الموافقة وتفعيل التنبيهات ✅" if action == "approve" else "تم رفض الطلب.") if changed else "تم التعامل مع هذا الطلب سابقاً."
            notice = ("تمت الموافقة على انضمامك وتفعيل التنبيهات ✅\n"
                      "يمكنك الآن استخدام /signals، ولإيقاف التنبيهات أرسل /alerts_off."
                      if action == "approve" else "لم تتم الموافقة على طلب انضمامك.")
        # Durable decisions survive notification failures and repeated clicks.
        api(token, "answerCallbackQuery", {"callback_query_id": callback["id"], "text": reply})
        if changed:
            try:
                api(token, "sendMessage", {"chat_id": chat_id, "text": notice})
            except TelegramError as error:
                logger.warning("Access decision notification failed; HTTP status=%s", error.status)
        return
    api(token, "answerCallbackQuery", {"callback_query_id": callback["id"], "text": reply})


def handle_update(token, update, store=None, worker_alive=True, history=None, reports=None,
                  gate_store=None, gate_worker_alive=True):
    if not isinstance(update, dict):
        return
    if store is not None and store.access_required() is True and "callback_query" in update:
        handle_access_callback(token, update["callback_query"], store)
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
    if command == "/myid":
        sender = message.get("from", {})
        if (isinstance(chat, dict) and chat.get("type") == "private"
                and type(chat.get("id")) is int and chat["id"] > 0
                and isinstance(sender, dict) and sender.get("id") == chat["id"]):
            api(token, "sendMessage", {"chat_id": chat["id"], "text": f"رقم حسابك في تيليجرام:\n{chat['id']}"})
        return
    if command == "/report":
        sender = message.get("from", {})
        allowed = (store is not None and isinstance(chat, dict) and chat.get("type") == "private"
                   and isinstance(sender, dict) and store.is_admin(sender.get("id")) is True
                   and type(chat.get("id")) is int and chat["id"] == sender.get("id"))
        if not allowed:
            if isinstance(chat, dict) and type(chat.get("id")) is int:
                api(token, "sendMessage", {"chat_id": chat["id"], "text": "تقرير Excel متاح للمدير فقط في محادثة خاصة."})
            return
        reply = "إعداد التقارير غير متاح الآن؛ حاول لاحقاً."
        if reports is not None:
            reply = "جارٍ تجهيز تقرير Excel لسجل الإشارات المرسلة إليك، مع الأسعار الحالية ونسبة التغيّر."
        api(token, "sendMessage", {"chat_id": chat["id"], "text": reply})
        if reports is not None:
            reports.request(chat["id"])
        return
    protected = ("/start", "/alerts_on", "/alerts_off", "/status", "/signals", "/requests", "/members", "/approve")
    if store is not None and store.access_required() is True and command in protected:
        if not isinstance(chat, dict) or type(chat.get("id")) is not int:
            return
        chat_id = chat["id"]
        sender = message.get("from", {})
        if (chat.get("type") != "private" or chat_id <= 0
                or not isinstance(sender, dict) or sender.get("id") != chat_id):
            api(token, "sendMessage", {"chat_id": chat_id, "text": "افتح محادثة خاصة مع البوت لإدارة اشتراكك."})
            return
        if command in ("/requests", "/members", "/approve"):
            if not store.is_admin(chat_id):
                api(token, "sendMessage", {"chat_id": chat_id, "text": "هذا الأمر متاح للمدير فقط."})
                return
            if command == "/approve":
                target = parts[1].strip() if len(parts) > 1 else ""
                if (not target.isascii() or not target.isdecimal() or len(target) > 16
                        or not 0 < int(target) < 2**52):
                    reply = "للموافقة على طلب سابق أرسل /approve ثم رقم الحساب."
                elif store.decide_access(int(target), True, int(time.time() * 1000), reconsider=True):
                    reply = "تمت الموافقة وتفعيل التنبيهات ✅"
                    try:
                        api(token, "sendMessage", {"chat_id": int(target), "text": "تمت الموافقة على انضمامك وتفعيل التنبيهات ✅\nيمكنك استخدام /signals."})
                    except TelegramError as error:
                        logger.warning("Access decision notification failed; HTTP status=%s", error.status)
                else:
                    reply = "لا يوجد طلب يحتاج إلى موافقة لهذا الحساب."
                api(token, "sendMessage", {"chat_id": chat_id, "text": reply})
                return
            member = command == "/members"
            rows = store.access_requests('approved' if member else 'pending')
            page_arg = parts[1].strip() if len(parts) > 1 else "1"
            page = int(page_arg) if page_arg.isascii() and page_arg.isdecimal() and len(page_arg) <= 6 else 1
            page = max(1, min(page, max(1, (len(rows) + 9) // 10)))
            if not rows:
                api(token, "sendMessage", {"chat_id": chat_id, "text": "لا يوجد مشتركون معتمدون." if member else "لا توجد طلبات معلّقة."})
            if rows:
                lines = ["المشتركون المعتمدون:" if member else "طلبات الانضمام:"]
                buttons = []
                for serial, (target, name) in enumerate(rows[(page - 1) * 10:page * 10], (page - 1) * 10 + 1):
                    lines.append(f"{serial}. {name}\nرقم الحساب: {target}")
                    buttons.extend(access_buttons(target, member, serial)["inline_keyboard"])
                if page * 10 < len(rows):
                    lines.append(f"الصفحة التالية: {command} {page + 1}")
                api(token, "sendMessage", {"chat_id": chat_id, "text": "\n\n".join(lines),
                    "reply_markup": {"inline_keyboard": buttons}})
            return
        if command != "/alerts_off" and not store.can_access(chat_id):
            name = " ".join(str(sender.get(key, "")) for key in ("first_name", "last_name")).strip()[:100] or "مستخدم"
            username = sender.get("username")
            if isinstance(username, str):
                name += " @" + username[:40]
            status, notified = store.request_access(chat_id, name, int(time.time() * 1000))
            reply = ("لم تتم الموافقة على طلبك؛ قراءة الإشارات تتطلب موافقة المدير." if status == 'denied'
                     else "تم تسجيل طلب انضمامك ⏳\nستتمكن من قراءة الإشارات واستقبال التنبيهات بعد موافقة المدير.")
            if store.admin_id is None:
                reply += "\nإعداد حساب المدير لم يكتمل بعد."
            api(token, "sendMessage", {"chat_id": chat_id, "text": reply})
            if status == 'pending' and not notified:
                notify_access_request(token, store, chat_id, name)
            return
    if command in ("/start", "/alerts_on", "/alerts_off", "/status", "/signals"):
        if not isinstance(chat, dict) or not isinstance(chat.get("id"), int):
            return
    if command == "/start":
        reply = WELCOME
        if gate_store is not None:
            reply = reply.replace("أراقب أزواج USDT في Binance Spot على فريم 4h.",
                                  "أراقب Binance Spot وأزواج Gate.io Spot غير الموجودة عليه، مقابل USDT وعلى فريم 4h.")
        if store is not None and store.access_required() is True and store.is_admin(chat["id"]):
            reply += "\n\nإدارة الاشتراكات:\n/requests — طلبات الانضمام\n/members — المشتركون وسحب الصلاحية\n/approve مع رقم الحساب — الموافقة على طلب سبق رفضه\n/report — تقرير Excel للإشارات والأسعار"
        api(token, "sendMessage", {"chat_id": message["chat"]["id"], "text": reply})
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
                if history.cached_result() is not None:
                    acknowledgement = "جارٍ عرض آخر تقرير محفوظ لآخر 48 ساعة؛ يُحدَّث التقرير في الخلفية."
                else:
                    acknowledgement = (
                        "يجري تجهيز التقرير الأول لآخر 48 ساعة في الخلفية؛ قد يستغرق بضع دقائق.\n"
                        "ستصل القائمة عند توفرها، ثم تُحفظ لتظهر بسرعة في الطلبات التالية."
                    )
                api(token, "sendMessage", {"chat_id": chat_id, "text": (
                    acknowledgement + "\nهذه إشارات تاريخية وليست تنبيهات جديدة."
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
                label = datetime.fromtimestamp(last_scan / 1000, DISPLAY_TZ).strftime("%Y-%m-%d (%H:%M:%S)")
                reply += f"\nآخر دورة فحص: {label}"
            if state.get("pending", 0):
                reply += "\nتوجد تنبيهات معلّقة تنتظر إعادة محاولة الإرسال."
            if history is not None and history.last_error:
                reply += "\n" + history.last_error
            if history is not None and isinstance(getattr(history, "refresh_error", None), str):
                reply += "\n" + history.refresh_error
            if gate_store is not None:
                secondary = gate_store.status()
                gate_labels = {
                    "idle": "تنتظر تفعيل التنبيهات", "ready": "آخر دورة مكتملة",
                    "scanning": "جارٍ الفحص", "blocked": "تعذر إكمال الفحص؛ ستتم إعادة المحاولة",
                    "backoff": "انتظار مهلة Gate.io", "error": "تعذر إكمال دورة الفحص",
                }
                gate_state = gate_labels.get(secondary.get("state"), "قيد التهيئة")
                if not gate_worker_alive:
                    gate_state = "عامل المراقبة متوقف؛ يلزم فحص التشغيل"
                checked, total = secondary.get("symbols_checked", 0), secondary.get("symbols_total", "—")
                reply += f"\n\nGate.io Spot — غير الموجودة على Binance:\n{gate_state}\nالأزواج: {checked}/{total}"
                stamp = secondary.get("last_scan_ms")
                if type(stamp) in (int, float):
                    from alerts import DISPLAY_TZ
                    label = datetime.fromtimestamp(stamp / 1000, DISPLAY_TZ).strftime("%Y-%m-%d (%H:%M:%S)")
                    reply += f"\nآخر دورة Gate.io: {label}"
        api(token, "sendMessage", {"chat_id": chat_id, "text": reply})


def process_updates(token, updates, store, worker_alive=True, history=None, reports=None,
                    gate_store=None, gate_worker_alive=True):
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
            handle_update(token, update, store, worker_alive, history, reports, gate_store, gate_worker_alive)
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
        from reporting import ReportService

        flag = os.environ.get("GATE_SPOT_ENABLED", "false").strip().lower()
        if flag not in ("true", "1", "on", "yes", "false", "0", "off", "no", ""):
            logger.error("GATE_SPOT_ENABLED must be true or false.")
            return 1
        gate_enabled = flag in ("true", "1", "on", "yes")
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
        raw_admin = os.environ.get("TELEGRAM_ADMIN_ID", "").strip()
        if raw_admin and (not raw_admin.isascii() or not raw_admin.isdecimal() or len(raw_admin) > 16):
            raise ValueError("TELEGRAM_ADMIN_ID must be a positive Telegram user ID.")
        store.configure_access(int(raw_admin) if raw_admin else None)
        if store.admin_id is None:
            logger.warning("Approval access is locked until TELEGRAM_ADMIN_ID is configured; /myid remains available.")
        gate_store = gate_client = None
        gate_path = (":memory:" if state_path == ":memory:" else
                     os.path.join(os.path.dirname(os.path.abspath(state_path)), "gate-alerts.sqlite3"))
        if gate_enabled or (gate_path != ":memory:" and os.path.isfile(gate_path)):
            from gate_market import GateClient
            from gate_runtime import GateStore
            gate_store = GateStore(gate_path, store)
            gate_client = GateClient(client, base_url=os.environ.get("API_GATE_BASE") or "https://api.gateio.ws")
            if gate_enabled:
                gate_store.eligible_symbol = gate_client.is_eligible
        stop = threading.Event()
        delivery_lock = threading.Lock()
        next_message_at = 0.0

        def send_alert(chat_id, text, parse_mode=None, *, signal_close_time=None,
                       source_store=None, source_client=None):
            nonlocal next_message_at
            source_store = source_store if source_store is not None else store
            source_client = source_client if source_client is not None else client
            # All platforms, history pages and reports share one paced sender.
            with delivery_lock:
                ensure_signal_access(source_store, chat_id)
                remaining = store.get_delivery_blocked_until() - time.time()
                if remaining > 0:
                    raise AlertDeliveryError("إرسال الرسائل ينتظر مهلة تيليجرام.",
                                             status=429, retry_after=remaining)
                def pace():
                    nonlocal next_message_at
                    delay = max(0, next_message_at - time.monotonic())
                    if stop.wait(delay):
                        raise AlertDeliveryError("توقف إرسال الرسائل.")
                    ensure_signal_access(source_store, chat_id)
                    if signal_close_time is not None:
                        native_symbol = text.split("\n", 1)[0].removeprefix("📈 ")
                        if not source_store.is_pending(chat_id, native_symbol, signal_close_time):
                            raise AlertDeliveryError("الإشارة لم تعد ضمن الإرسال المسموح.", status=403)
                    next_message_at = time.monotonic() + 1.05
                    store.set_next_delivery_at(time.time() + 1.05)

                try:
                    send_alert_message(token, source_client, chat_id, text, parse_mode, before_send=pace,
                                       store=source_store if signal_close_time is not None else None,
                                       signal_close_time=signal_close_time)
                except TelegramError as error:
                    if error.status == 429:
                        store.set_delivery_blocked_until(time.time() + max(60, error.retry_after or 0))
                    raise AlertDeliveryError(str(error), status=error.status,
                                             retry_after=error.retry_after) from None

        def send_live_signal(chat_id, symbol, close_time, text):
            if text.split("\n", 1)[0] != "📈 " + symbol:
                raise ValueError("Signal heading and delivery identity differ.")
            return send_alert(chat_id, text, signal_close_time=close_time)

        def send_report_document(chat_id, content, filename, caption):
            nonlocal next_message_at
            with delivery_lock:
                if not store.is_admin(chat_id):
                    raise AlertDeliveryError("التقرير متاح للمدير فقط.", status=403)
                remaining = store.get_delivery_blocked_until() - time.time()
                if remaining > 0:
                    raise AlertDeliveryError("الإرسال ينتظر مهلة تيليجرام.", status=429, retry_after=remaining)
                if stop.wait(max(0, next_message_at - time.monotonic())):
                    raise AlertDeliveryError("توقف إرسال التقرير.")
                if not store.is_admin(chat_id):
                    raise AlertDeliveryError("التقرير متاح للمدير فقط.", status=403)
                next_message_at = time.monotonic() + 1.05
                store.set_next_delivery_at(time.time() + 1.05)
                try:
                    return send_document(token, chat_id, content, filename, caption)
                except TelegramError as error:
                    if error.status == 429:
                        store.set_delivery_blocked_until(time.time() + max(60, error.retry_after or 0))
                    raise AlertDeliveryError(str(error), status=error.status, retry_after=error.retry_after) from None

        def send_report_notice(chat_id, text):
            if not store.is_admin(chat_id):
                raise AlertDeliveryError("التقرير متاح للمدير فقط.", status=403)
            return send_alert(chat_id, text)

        scanner = Scanner(client, store, send_alert, send_signal=send_live_signal)
        history = HistoryService(client, lambda chat_id, text: send_alert(chat_id, text, "HTML"), stop,
                                 store=store)
        gate_scanner = None
        if gate_enabled:
            from gate_runtime import CombinedHistory
            def send_gate_signal(chat_id, symbol, close_time, text):
                if text.split("\n", 1)[0] != "📈 " + symbol:
                    raise ValueError("Gate signal heading and identity differ.")
                return send_alert(chat_id, text, signal_close_time=close_time,
                                  source_store=gate_store, source_client=gate_client)
            gate_scanner = Scanner(gate_client, gate_store, send_alert, send_signal=send_gate_signal)
            gate_history = HistoryService(gate_client,
                lambda chat_id, text: send_alert(chat_id, text, "HTML"), stop,
                store=gate_store, exchange_name="Gate.io Spot")
            history = CombinedHistory(history, gate_history)
        report_store, report_client = store, client
        if gate_store is not None:
            from gate_runtime import ReportStore, MarketRouter
            report_store, report_client = ReportStore(store, gate_store), MarketRouter(client, gate_client)
        reports = ReportService(report_store, report_client, send_report_document, send_report_notice, stop)
        history.start_background()
        try:
            api(token, "setMyCommands", {"commands": [
                {"command": "start", "description": "شرح البوت والفلتر"},
                {"command": "signals", "description": "عرض جميع إشارات آخر 48 ساعة"},
                {"command": "alerts_on", "description": "تفعيل التنبيهات"},
                {"command": "alerts_off", "description": "إيقاف التنبيهات"},
                {"command": "status", "description": "حالة المراقبة والتنبيهات"},
                {"command": "myid", "description": "عرض رقم حسابك في تيليجرام"},
                {"command": "report", "description": "تقرير Excel للإشارات والأسعار — للمدير"},
            ]})
        except TelegramError as error:
            logger.warning("Command menu refresh failed; HTTP status=%s", error.status)

        def monitor(active_scanner):
            while not stop.is_set():
                try:
                    active_scanner.run(stop)
                except Exception as error:
                    logger.error("Scanner worker will restart; exception type=%s", type(error).__name__)
                if not stop.is_set():
                    stop.wait(30)

        worker = threading.Thread(target=monitor, args=(scanner,), name="market-scanner", daemon=True)
        worker.start()
        gate_worker = None
        if gate_scanner is not None:
            gate_worker = threading.Thread(target=monitor, args=(gate_scanner,), name="gate-scanner", daemon=True)
            gate_worker.start()
        print("البوت يعمل وينتظر الرسائل.", flush=True)
        offset = store.get_update_offset()
        next_health_log = 0.0
        try:
            while True:
                try:
                    updates = api(token, "getUpdates", {"offset": offset, "timeout": 30, "allowed_updates": ["message", "callback_query"]})
                    offset = process_updates(token, updates, store, worker.is_alive(), history, reports,
                                             gate_store if gate_enabled else None,
                                             gate_worker.is_alive() if gate_worker is not None else True)
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
            deadline = time.monotonic() + 50
            workers = [worker] + ([gate_worker] if gate_worker is not None else [])
            for thread in workers:
                thread.join(timeout=min(25, max(0, deadline - time.monotonic())))
            history.join(timeout=min(25, max(0, deadline - time.monotonic())))
            reports.join(timeout=min(5, max(0, deadline - time.monotonic())))
            if not any(thread.is_alive() for thread in workers) and not history.is_alive() and not reports.is_alive():
                if gate_store is not None:
                    gate_store.close()
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
