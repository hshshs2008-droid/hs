import unittest
from alerts import AlertStore
from daily_runtime import DailyReports
from gate_runtime import ReportStore
from reporting import ReportView, build_report
from test_reporting import cells


class SharedReportTests(unittest.TestCase):
    def test_common_history_deduplicates_recipients_and_keeps_frames_and_prices(self):
        main, daily = AlertStore(':memory:'), AlertStore(':memory:')
        self.addCleanup(main.close)
        self.addCleanup(daily.close)
        main.configure_access(100)
        main.request_access(200, 'New member', 100000)
        main.decide_access(200, True, 100001)
        main._set_metadata('report_excluded_legacy_global_v1', [])
        daily.exchange_name = 'Binance Spot'
        def sent(store, chat, price, timestamp, close=1000):
            attempt = store.prepare_delivery(chat, 'BTCUSDT', close, price, timestamp,
                                             'PRIVATE MESSAGE', timestamp)
            store.confirm_delivery(attempt, 1, timestamp, timestamp)
        sent(main, 100, '10', 2000)
        sent(main, 300, '12', 3000)
        sent(main, 100, '13', 4000)  # retry is not a separate recommendation
        sent(daily, 300, '20', 5000)  # same coin/close but distinct timeframe
        main.prepare_delivery(100, 'FAILEDUSDT', 1000, '1', 2000, 'secret', 2000)
        view = ReportView(DailyReports(ReportStore(main), [('binance', daily)]), main)
        owner, subscriber = view.report_rows(100), view.report_rows(200)
        self.assertEqual(owner, subscriber)
        self.assertEqual(len(owner), 2)
        self.assertEqual({r.get('timeframe', '4h'): r['quote_price'] for r in owner},
                         {'4h': '10', '1D': '20'})
        for row in owner:
            self.assertFalse({'chat_id','message_text','telegram_message_id','attempt_id'} & row.keys())
        data = build_report(subscriber, {('binance','BTCUSDT'): {'price':'25','at_ms':100002}}, 100002)
        self.assertEqual(len(cells(data)), 3)
        self.assertNotIn('PRIVATE MESSAGE', str(cells(data, 'sheet2')))
        self.assertEqual(view.report_rows(400), [])
        main.revoke_access(200)
        self.assertEqual(view.report_rows(200), [])
