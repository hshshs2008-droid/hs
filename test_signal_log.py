import unittest
from unittest.mock import Mock, patch
from signal_log import SignalLog
from alerts import AlertStore

class SignalLogTests(unittest.TestCase):
    def test_shared_order_window_format_and_no_background(self):
        view, send, api = Mock(), Mock(), Mock()
        view.can_access.return_value = True
        now = 1000000000
        def row(symbol, sent, frame):
            return dict(symbol=symbol, quote_key=('gate',symbol), signal_close_time=now-1,
                        telegram_sent_at_ms=sent,timeframe=frame)
        view.report_rows.return_value = [row('NEW_USDT',now-1,'4h'),row('OLD_USDT',now-100,'1D'),row('EXPIRED_USDT',0,'4h')]
        api.return_value = {'username':'testbot'}
        log = SignalLog(view,{},send,api,'token')
        with patch('signal_log.time.time',return_value=now/1000): log.request(100)
        text=send.call_args.args[1]
        self.assertLess(text.index('OLDUSDT'),text.index('NEWUSDT'))
        self.assertNotIn('EXPIRED',text)
        self.assertIn('1D | <a href=',text)
        self.assertIn('symbol=GATEIO%3AOLDUSDT&amp;interval=D',text)
        self.assertIn('symbol=GATEIO%3ANEWUSDT&amp;interval=240',text)
        self.assertIn('?start=sig_',text)
        self.assertNotIn('Spot',text)
        self.assertFalse(log.start_background())
        view.can_access.return_value=False
        send.reset_mock(); log.request(100); send.assert_not_called()

    def test_pagination_counts_visible_text_and_preserves_all_links(self):
        from html import unescape
        import re
        view, send, api = Mock(), Mock(), Mock()
        view.can_access.return_value = True
        api.return_value = {'username': 'testbot'}
        now = 1000000000
        for count in (30, 200):
            send.reset_mock()
            view.report_rows.return_value = [dict(symbol=f'COIN{i:03d}_USDT',
                quote_key=('gate', f'COIN{i:03d}_USDT'), signal_close_time=now-1,
                telegram_sent_at_ms=now-count+i, timeframe='4h') for i in range(count)]
            log = SignalLog(view, {}, send, api, 'token')
            with patch('signal_log.time.time', return_value=now/1000):
                log.request(100)
            pages = [call.args[1] for call in send.call_args_list]
            self.assertEqual(sum(p.count('?start=sig_') for p in pages), count)
            for page in pages:
                visible = unescape(re.sub('<[^>]+>', '', page))
                self.assertLessEqual(len(visible.encode('utf-16-le'))//2, 4096)
            if count == 30:
                self.assertEqual(len(pages), 1)
                self.assertGreater(len(pages[0]), 3500)
            else:
                self.assertGreater(len(pages), 1)

    def test_detail_replies_only_to_requesters_original_and_new_member_gets_details(self):
        store=AlertStore(':memory:'); self.addCleanup(store.close)
        attempt=store.prepare_delivery(100,'BTCUSDT',1000,'1',2000,'📈 BTCUSDT\n📊 RSI (14): 29 → 31\n📊 Last alert: private',2000)
        store.confirm_delivery(attempt,22,2000,2000)
        view=Mock();view.can_access.return_value=True
        row=store.shared_report_rows()[0];row['quote_key']=('binance','BTCUSDT')
        view.report_rows.return_value=[row]
        api=Mock();log=SignalLog(view,{('binance','4h'):store},Mock(),api,'token')
        log.detail(100,log.key(row))
        self.assertEqual(api.call_args.args[2]['reply_parameters']['message_id'],22)
        log.detail(200,log.key(row))
        payload=api.call_args.args[2]
        self.assertNotIn('reply_parameters',payload)
        self.assertNotIn('Last alert:',payload['text'])
        self.assertIn('🔔 BTCUSDT',payload['text'])
        api.reset_mock();view.can_access.return_value=False
        log.detail(200,log.key(row));api.assert_not_called()
