import unittest
import io
import json
from http.client import IncompleteRead
from unittest.mock import Mock, patch

import bot


class OffsetStore:
    def __init__(self, offset=0):
        self.offset = offset

    def get_update_offset(self):
        return self.offset

    def set_update_offset(self, offset):
        self.offset = offset


class BotTests(unittest.TestCase):
    def test_truncated_telegram_response_is_recoverable_and_sanitized(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.side_effect = IncompleteRead(b"secret-token", 100)
        with patch("bot.urlopen", return_value=response):
            with self.assertRaises(bot.TelegramError) as raised:
                bot.api("secret-token", "getMe")
        self.assertNotIn("secret-token", str(raised.exception))
        with patch("bot.urlopen", return_value=io.BytesIO(b'{"ok":true,"result":{"is_bot":true}}')):
            self.assertTrue(bot.api("secret-token", "getMe")["is_bot"])

    def test_malformed_telegram_response_is_retryable(self):
        for result in ([], None, {"ok": True}):
            with self.subTest(result=result), patch("bot.urlopen", return_value=io.BytesIO(json.dumps(result).encode())):
                with self.assertRaises(bot.TelegramError):
                    bot.api("test-token", "getMe")

    @patch("bot.handle_update")
    def test_permanent_reply_failure_does_not_block_later_updates(self, handler):
        store = OffsetStore(10)
        handler.side_effect = [bot.TelegramError("private raw failure", status=403), None]
        with self.assertLogs("bot", level="WARNING") as log:
            end = bot.process_updates("test-token", [{"update_id": 10}, {"update_id": 11}], store)
        self.assertEqual(end, 12)
        self.assertEqual(store.offset, 12)
        self.assertEqual(handler.call_count, 2)
        self.assertNotIn("private raw failure", " ".join(log.output))

    @patch("bot.handle_update")
    def test_transient_reply_failure_preserves_failed_update_for_retry(self, handler):
        store = OffsetStore(10)
        handler.side_effect = [None, bot.TelegramError("Retry later", status=429, retry_after=120)]
        updates = [{"update_id": 10}, {"update_id": 11}, {"update_id": 12}]
        with self.assertRaises(bot.TelegramError):
            bot.process_updates("test-token", updates, store)
        self.assertEqual(store.offset, 11)
        handler.reset_mock(side_effect=True)
        bot.process_updates("test-token", updates, store)
        self.assertEqual(store.offset, 13)
        self.assertEqual([c.args[1]["update_id"] for c in handler.call_args_list], [11, 12])

    @patch("bot.api")
    def test_whitespace_and_malformed_messages_do_not_crash(self, api):
        for update in [None, [], {"message": None}, {"message": {"text": " \n\t "}},
                       {"message": {"text": 42}}, {"message": {"text": "/start"}}]:
            bot.handle_update("test-token", update)
        api.assert_not_called()

    @patch("bot.api")
    def test_start_commands(self, api):
        for text in ["/start", "/start referral", "/start@my_bot"]:
            bot.handle_update("test-token", {"message": {"text": text, "chat": {"id": 42}}})
            api.assert_called_with("test-token", "sendMessage", {"chat_id": 42, "text": bot.WELCOME})
        self.assertEqual(api.call_count, 3)

    @patch("bot.api")
    def test_other_updates_are_ignored(self, api):
        for update in [{}, {"message": {"photo": []}}, {"message": {"text": "hello"}}]:
            bot.handle_update("test-token", update)
        api.assert_not_called()

    @patch.dict("os.environ", {}, clear=True)
    def test_missing_token(self):
        self.assertEqual(bot.main(), 1)

    @patch("bot.api")
    @patch("bot.time.time", return_value=1000)
    def test_private_opt_in(self, clock, api):
        store = Mock()
        bot.handle_update("test-token", {"message": {"text": "/alerts_on", "chat": {"id": 42, "type": "private"}}}, store)
        store.subscribe.assert_called_once_with(42, 1000000)
        self.assertIn("تم تفعيل", api.call_args.args[2]["text"])

    @patch("bot.api")
    def test_group_cannot_subscribe(self, api):
        store = Mock()
        bot.handle_update("test-token", {"message": {"text": "/alerts_on", "chat": {"id": -42, "type": "group"}}}, store)
        store.subscribe.assert_not_called()
        self.assertIn("محادثة خاصة", api.call_args.args[2]["text"])

    @patch("bot.api")
    def test_unsubscribe(self, api):
        store = Mock()
        bot.handle_update("test-token", {"message": {"text": "/alerts_off", "chat": {"id": 42, "type": "private"}}}, store)
        store.unsubscribe.assert_called_once_with(42)

    @patch("bot.api")
    def test_status_does_not_claim_blocked_monitor_ready(self, api):
        store = Mock()
        store.subscribers.return_value = [(42, 0)]
        store.status.return_value = {"state": "blocked", "symbols_checked": 0}
        bot.handle_update("test-token", {"message": {"text": "/status", "chat": {"id": 42, "type": "private"}}}, store)
        self.assertIn("تعذر جلب", api.call_args.args[2]["text"])

    def test_failed_send_does_not_expose_token(self):
        from urllib.error import URLError
        with patch("bot.urlopen", side_effect=URLError("secret-token in request URL")):
            with self.assertRaises(bot.TelegramError) as raised:
                bot.api("secret-token", "getMe")
            self.assertNotIn("secret-token", str(raised.exception))

    def test_telegram_rate_limit_retains_cooldown(self):
        from io import BytesIO
        from urllib.error import HTTPError
        error = HTTPError("https://api.telegram.org/botsecret-token/sendMessage", 429, "Too many requests", {},
                          BytesIO(b'{"ok":false,"parameters":{"retry_after":125}}'))
        with patch("bot.urlopen", side_effect=error):
            with self.assertRaises(bot.TelegramError) as raised:
                bot.api("secret-token", "sendMessage")
        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(raised.exception.retry_after, 125)
        self.assertNotIn("secret-token", str(raised.exception))

    @patch("bot.api")
    def test_status_reports_pending_deliveries(self, api):
        store = Mock()
        store.subscribers.return_value = [(42, 0)]
        store.status.return_value = {"state": "ready", "symbols_checked": 500, "pending": 1}
        bot.handle_update("test-token", {"message": {"text": "/status", "chat": {"id": 42, "type": "private"}}}, store)
        self.assertIn("تنبيهات معلّقة", api.call_args.args[2]["text"])

    @patch("bot.api")
    def test_history_request_is_private_and_does_not_subscribe(self, api):
        store = Mock()
        history = Mock()
        bot.handle_update("test-token", {"message": {"text": "/signals", "chat": {"id": 42, "type": "private"}}},
                          store, history=history)
        history.request.assert_called_once_with(42)
        store.subscribe.assert_not_called()
        store.record.assert_not_called()
        self.assertIn("إشارات تاريخية", api.call_args.args[2]["text"])

    @patch("bot.api")
    def test_group_cannot_request_history(self, api):
        store = Mock()
        history = Mock()
        bot.handle_update("test-token", {"message": {"text": "/signals", "chat": {"id": -42, "type": "group"}}},
                          store, history=history)
        history.request.assert_not_called()

    @patch("bot.api", side_effect=bot.TelegramError("Unavailable"))
    def test_failed_acknowledgement_does_not_schedule_history(self, api):
        history = Mock()
        with self.assertRaises(bot.TelegramError):
            bot.handle_update("test-token", {"message": {"text": "/signals", "chat": {"id": 42, "type": "private"}}},
                              Mock(), history=history)
        history.request.assert_not_called()

    @patch("bot.api")
    def test_status_displays_history_delivery_failure(self, api):
        store = Mock()
        store.subscribers.return_value = [(42, 0)]
        store.status.return_value = {"state": "ready", "pending": 0}
        history = Mock()
        history.last_error = "تعذر إرسال تقرير الإشارات كاملاً؛ أعد /signals لاحقاً."
        bot.handle_update("test-token", {"message": {"text": "/status", "chat": {"id": 42, "type": "private"}}},
                          store, history=history)
        self.assertIn(history.last_error, api.call_args.args[2]["text"])


if __name__ == "__main__":
    unittest.main()
