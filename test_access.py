"""Approval boundaries use real SQLite and mocked Telegram/market calls."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import bot
from alerts import AlertDeliveryError, AlertStore
from history import HistoryService
from indicators import Signal


class AccessTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'state.sqlite3'
        self.store = AlertStore(self.path)
        self.addCleanup(self.store.close)
        self.store.configure_access(100)

    def command(self, text, user=200, **kwargs):
        update = {'message': {'text': text, 'chat': {'id': user, 'type': 'private'},
                             'from': {'id': user, 'first_name': 'Visitor'}}}
        bot.handle_update('test-token', update, self.store, **kwargs)

    def callback(self, action='approve', user=100, target=200, chat=None):
        update = {'callback_query': {'id': 'test-callback', 'from': {'id': user},
                  'message': {'chat': {'id': user if chat is None else chat, 'type': 'private'}},
                  'data': f'{action}:{target}'}}
        bot.handle_update('test-token', update, self.store)

    def request(self, user=200):
        self.store.request_access(user, 'Visitor', 1000)

    @patch('bot.api')
    def test_unknown_user_cannot_read_subscribe_or_inspect_status(self, api):
        history = Mock()
        for command in ('/start', '/signals', '/alerts_on', '/status'):
            self.command(command, history=history)
            replies = [call.args[2]['text'] for call in api.call_args_list if call.args[2].get('chat_id') == 200]
            self.assertIn('طلب انضمامك', replies[-1])
        history.request.assert_not_called()
        self.assertEqual(self.store.subscribers(), [])
        self.assertEqual(self.store.access_requests(), [(200, 'Visitor')])
        notices = [call for call in api.call_args_list if call.args[2].get('chat_id') == 100]
        self.assertEqual(len(notices), 1)
        buttons = notices[0].args[2]['reply_markup']['inline_keyboard'][0]
        self.assertEqual([button['callback_data'] for button in buttons], ['approve:200', 'deny:200'])

    @patch('bot.api')
    def test_approval_enables_history_and_alerts_without_reactivating_on_duplicate_click(self, api):
        self.request()
        with patch('bot.time.time', return_value=10):
            self.callback()
        self.assertTrue(self.store.can_access(200))
        self.assertEqual(self.store.subscribers(), [(200, 10000)])
        with patch('bot.time.time', return_value=20):
            self.callback()
        self.assertEqual(self.store.subscribers(), [(200, 10000)])
        history = Mock()
        self.command('/signals', history=history)
        history.request.assert_called_once_with(200)

    @patch('bot.api')
    def test_forged_admin_callbacks_and_commands_cannot_grant_or_list_access(self, api):
        self.request()
        for user, chat in ((200, 200), (200, 100), (100, -1)):
            self.callback(user=user, chat=chat)
            self.assertFalse(self.store.can_access(200))
        for command in ('/requests', '/members', '/approve 200'):
            self.command(command)
            self.assertIn('للمدير فقط', api.call_args.args[2]['text'])
        self.assertFalse(self.store.can_access(200))

    @patch('bot.api')
    def test_denial_blocks_requests_and_stale_buttons_but_owner_can_reconsider(self, api):
        self.request()
        self.callback('deny')
        self.callback('approve')
        self.assertFalse(self.store.can_access(200))
        self.command('/signals')
        self.assertIn('لم تتم الموافقة', api.call_args.args[2]['text'])
        self.command('/approve 200', user=100)
        self.assertTrue(self.store.can_access(200))

    @patch('bot.api')
    def test_revocation_removes_pending_deliveries_and_blocks_cached_history(self, api):
        self.request()
        self.store.decide_access(200, True, 1000)
        signal = Signal(2000, 19, 21, 10, 23, 14, 21, 2000, 2000)
        self.store.record('BTCUSDT', 2000, signal)
        self.assertEqual(len(self.store.pending()), 1)
        sender = Mock()
        service = HistoryService(Mock(), sender, threading.Event(), store=self.store)
        self.callback('revoke')
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.subscribers(), [])
        self.assertFalse(self.store.is_pending(200, 'BTCUSDT', 2000))
        self.assertFalse(service.request(200))
        self.assertTrue(service._send_pages(200, ['cached report']))
        sender.assert_not_called()
        with self.assertRaises(AlertDeliveryError):
            bot.ensure_signal_access(self.store, 200)
        self.callback('approve')  # Old buttons cannot restore revoked access.
        self.assertFalse(self.store.can_access(200))

    def test_permissions_pending_requests_owner_and_subscription_survive_reopen(self):
        self.request()
        self.request(300)
        self.store.decide_access(200, True, 5000)
        self.store.set_update_offset(42)
        reopened = AlertStore(self.path)
        self.addCleanup(reopened.close)
        reopened.configure_access()  # Saved owner is retained without an environment value.
        self.assertTrue(reopened.is_admin(100))
        self.assertTrue(reopened.can_access(200))
        self.assertFalse(reopened.can_access(300))
        self.assertEqual(reopened.access_requests(), [(300, 'Visitor')])
        self.assertEqual(reopened.subscribers(), [(200, 5000)])
        self.assertEqual(reopened.get_update_offset(), 42)

    def test_existing_subscribers_are_not_grandfathered_and_keep_their_state(self):
        legacy = AlertStore(':memory:')
        self.addCleanup(legacy.close)
        legacy.subscribe(100, 1000)
        legacy.subscribe(200, 1000)
        legacy.record('BTCUSDT', 2000, Signal(2000, 19, 21, 10, 23, 14, 21, 2000, 2000))
        legacy.configure_access(100)
        self.assertEqual(legacy.subscribers(), [(100, 1000)])
        self.assertEqual([row[0] for row in legacy.pending()], [100])
        self.assertEqual(legacy.checkpoint('BTCUSDT'), 2000)
        legacy.request_access(200, 'Visitor', 3000)
        legacy.decide_access(200, True, 4000)
        self.assertEqual(legacy.pending(), [row for row in legacy.pending() if row[0] == 100])
        self.assertEqual(legacy.subscribers(), [(100, 1000), (200, 4000)])

    @patch('bot.api')
    def test_missing_owner_is_fail_closed_but_myid_works(self, api):
        unset = AlertStore(':memory:')
        self.addCleanup(unset.close)
        unset.configure_access()
        update = {'message': {'text': '/myid', 'chat': {'id': 200, 'type': 'private'}, 'from': {'id': 200}}}
        bot.handle_update('test-token', update, unset)
        self.assertIn('200', api.call_args.args[2]['text'])
        update['message']['text'] = '/signals'
        history = Mock()
        bot.handle_update('test-token', update, unset, history=history)
        history.request.assert_not_called()
        self.assertFalse(unset.can_access(200))
        self.assertIsNone(unset.admin_id)
        self.assertEqual(unset.subscribers(), [])

    @patch('bot.api')
    def test_owner_lists_requests_and_members_with_revoke_buttons(self, api):
        self.request()
        self.command('/requests', user=100)
        self.assertEqual(api.call_args.args[2]['reply_markup'], bot.access_buttons(200, serial=1))
        self.store.decide_access(200, True, 1000)
        self.command('/members', user=100)
        self.assertEqual(api.call_args.args[2]['reply_markup'], bot.access_buttons(200, True, serial=1))
        self.command('/alerts_off')
        self.assertTrue(self.store.can_access(200))
        self.assertEqual(self.store.subscribers(), [])

    @patch('bot.api')
    def test_group_and_mismatched_sender_cannot_create_access_requests(self, api):
        history = Mock()
        for chat, sender in (({'id': -200, 'type': 'group'}, 200), ({'id': 200, 'type': 'private'}, 300)):
            bot.handle_update('test-token', {'message': {'text': '/signals', 'chat': chat, 'from': {'id': sender}}},
                              self.store, history=history)
        self.assertEqual(self.store.access_requests(), [])
        history.request.assert_not_called()

    @patch('bot.api')
    def test_notification_failure_retains_pending_request_and_retry_does_not_duplicate(self, api):
        api.side_effect = [None, bot.TelegramError('blocked', status=403)]
        with self.assertLogs('bot', level='WARNING'):
            self.command('/start')
        self.assertEqual(self.store.access_requests(), [(200, 'Visitor')])
        self.assertFalse(self.store.request_access(200, 'Visitor', 2000)[1])
        api.side_effect = None
        self.command('/start')
        self.assertTrue(self.store.request_access(200, 'Visitor', 2000)[1])
        self.assertEqual(len(self.store.access_requests()), 1)

    @patch('bot.api')
    def test_unknown_stale_and_malformed_callback_data_never_change_access(self, api):
        self.request()
        for action, target in (('wrong', 200), ('approve', -200), ('approve', 'x'), ('approve', '9' * 5000), ('approve', 999)):
            self.callback(action, target=target)
        self.assertFalse(self.store.can_access(200))
        self.callback('deny', target=100)
        self.assertTrue(self.store.can_access(100))

    @patch('bot.api')
    def test_lists_paginate_without_hiding_members(self, api):
        for user in range(200, 222):
            self.request(user)
            self.store.decide_access(user, True, 1000)
        self.command('/members 3', user=100)
        displayed = [row[0]['callback_data'] for row in api.call_args.args[2]['reply_markup']['inline_keyboard']]
        self.assertEqual(displayed, ['revoke:220', 'revoke:221'])

    def test_revocation_between_report_pages_cancels_the_remaining_pages(self):
        self.request()
        self.store.decide_access(200, True, 1000)
        sent = []
        def send(chat_id, text):
            sent.append(text)
            self.store.revoke_access(chat_id)
        service = HistoryService(Mock(), send, threading.Event(), request_delay=0, store=self.store)
        self.assertTrue(service._send_pages(200, ['first', 'second']))
        self.assertEqual(sent, ['first'])

    def test_invalid_owner_values_are_rejected(self):
        for value in (True, 0, -1, '100', 2**52):
            with self.assertRaises(ValueError):
                self.store.configure_access(value)


if __name__ == '__main__':
    unittest.main()
