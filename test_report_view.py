"""Fixed report exclusions survive restart and never delete delivery history."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from alerts import AlertStore
from reporting import ReportView


class ReportViewTests(unittest.TestCase):
    def test_exactly_first99_are_fixed_across_restart_and_new_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.sqlite3'
            metadata = AlertStore(path)
            source = Mock()
            source.is_admin.return_value = True
            source.can_access.return_value = True
            rows = [dict(symbol='BTCUSDT', signal_close_time=i, timeframe='4h',
                         quote_key=('binance', 'BTCUSDT')) for i in range(1, 104)]
            source.report_rows.return_value = rows
            view = ReportView(source, metadata)
            self.assertEqual([r['signal_close_time'] for r in view.report_rows(100)], [100,101,102,103])
            self.assertEqual(len(rows), 103)
            metadata.close()
            metadata = AlertStore(path)
            try:
                # A different frame on the same close and new subsequent signals
                # must remain visible, never replace the frozen excluded set.
                rows.extend([dict(rows[0], timeframe='1D'), dict(rows[-1], signal_close_time=104)])
                restored = ReportView(source, metadata)
                visible = restored.report_rows(100)
                self.assertEqual(len(visible), 6)
                self.assertIn(rows[-2], visible)
                self.assertIn(rows[-1], visible)
                self.assertEqual(len(metadata._metadata('report_excluded_first99_v1:100', [])), 99)
            finally:
                metadata.close()

    def test_subscriber_gets_own_rows_without_owner_exclusions_and_revocation_blocks(self):
        from gate_runtime import ReportStore
        from daily_runtime import DailyReports
        store = AlertStore(':memory:')
        self.addCleanup(store.close)
        store.configure_access(100)
        store.request_access(200, 'Subscriber', 1)
        store.decide_access(200, True, 2)
        for chat, symbol in [(100, 'OWNERUSDT'), (200, 'MEMBERUSDT')]:
            attempt = store.prepare_delivery(chat, symbol, 1000, '1', 2000, 'payload', 2000)
            store.confirm_delivery(attempt, chat, 2000, 2000)
        view = ReportView(DailyReports(ReportStore(store), []), store)
        self.assertEqual([r['symbol'] for r in view.report_rows(200)], ['MEMBERUSDT'])
        self.assertIsNone(store._metadata('report_excluded_first99_v1:200', None))
        self.assertEqual(view.report_rows(100), [])
        store.revoke_access(200)
        self.assertFalse(view.can_access(200))
        self.assertEqual(view.report_rows(200), [])

    def test_unapproved_user_cannot_initialize_or_read_report_filter(self):
        source, metadata = Mock(), Mock()
        source.can_access.return_value = False
        self.assertEqual(ReportView(source, metadata).report_rows(101), [])
        source.report_rows.assert_not_called()
        metadata._set_metadata.assert_not_called()


if __name__ == '__main__':
    unittest.main()
