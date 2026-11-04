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

    def test_unapproved_user_cannot_initialize_or_read_report_filter(self):
        source, metadata = Mock(), Mock()
        source.is_admin.return_value = False
        self.assertEqual(ReportView(source, metadata).report_rows(101), [])
        source.report_rows.assert_not_called()
        metadata._set_metadata.assert_not_called()


if __name__ == '__main__':
    unittest.main()
