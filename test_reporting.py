from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
import threading
import time
import unittest
from unittest.mock import Mock
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from alerts import AlertDeliveryError
from market import MarketError
from reporting import HEADERS, ReportService, SHEET_NS, build_report


NS = {'s': SHEET_NS}
NOW = int(datetime(2026, 10, 25, 8, tzinfo=timezone.utc).timestamp() * 1000)


def row(symbol='BTCUSDT', entry='100', close=NOW-1, **kwargs):
    return dict(symbol=symbol, signal_close_time=close, quote_price=entry,
                quote_at_ms=NOW, telegram_sent_at_ms=NOW+1000, **kwargs)


def cells(document, sheet='sheet1'):
    with ZipFile(BytesIO(document)) as archive:
        tree = ET.fromstring(archive.read(f'xl/worksheets/{sheet}.xml'))
    results = []
    for source in tree.findall('s:sheetData/s:row', NS):
        values = []
        for cell in source.findall('s:c', NS):
            if cell.get('t') == 'inlineStr':
                values.append(cell.find('s:is/s:t', NS).text or '')
            else:
                values.append(Decimal(cell.find('s:v', NS).text))
        results.append(values)
    return results


class WorkbookTests(unittest.TestCase):
    def test_zip_workbook_is_well_formed_and_has_numeric_prices_and_changes(self):
        data = build_report([row()], {'BTCUSDT': {'price': '125', 'at_ms': NOW+2000}}, NOW+3000, NOW)
        with ZipFile(BytesIO(data)) as archive:
            self.assertIsNone(archive.testzip())
            for name in archive.namelist():
                ET.fromstring(archive.read(name))
            tree = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
            self.assertEqual(tree.find('s:sheetViews/s:sheetView', NS).get('rightToLeft'), '1')
            self.assertEqual(tree.find('s:sheetViews/s:sheetView/s:pane', NS).get('state'), 'frozen')
            self.assertEqual(tree.find('s:autoFilter', NS).get('ref'), 'A1:H2')
        table = cells(data)
        self.assertEqual(tuple(table[0]), HEADERS)
        self.assertEqual(table[1][1], 'BTC/USDT')
        self.assertEqual(table[1][2:5], ['Binance', '2026-10-25', '10:00'])
        self.assertEqual(table[1][5:], [Decimal('100'), Decimal('125'), Decimal('.25')])
        metadata = str(cells(data, 'sheet2'))
        self.assertIn('UTC', metadata)
        self.assertIn('2026-10-25 (08:00:02)', metadata)
        self.assertNotIn('chat_id', metadata)

    def test_openpyxl_can_read_without_repairs_when_available(self):
        try:
            from openpyxl import load_workbook
        except ImportError:
            self.skipTest('Optional compatibility reader not installed.')
        data = build_report([row()], {'BTCUSDT': {'price': '90', 'at_ms': NOW}}, NOW)
        workbook = load_workbook(BytesIO(data), data_only=True)
        self.addCleanup(workbook.close)
        sheet = workbook.worksheets[0]
        self.assertEqual(sheet['D2'].value, '2026-10-25')
        self.assertEqual(sheet['E2'].value, '10:00')
        self.assertEqual(sheet['F2'].value, 100)
        self.assertAlmostEqual(sheet['H2'].value, -.1)
        self.assertEqual(sheet.freeze_panes, 'A2')
        self.assertTrue(sheet.sheet_view.rightToLeft)

    def test_negative_tiny_and_repeated_signals_keep_numeric_precision(self):
        rows = [row('LOWUSDT', '0.000000000000000000000000000000000000001'),
                row(entry='200', close=NOW-1000), row(entry='100', close=NOW-2000)]
        quotes = {'LOWUSDT': {'price': '0.000000000000000000000000000000000000002', 'at_ms': NOW},
                  'BTCUSDT': {'price': '150', 'at_ms': NOW}}
        table = cells(build_report(rows, quotes, NOW))
        self.assertEqual([entry[1] for entry in table[1:]], ['BTC/USDT', 'BTC/USDT', 'LOW/USDT'])
        self.assertEqual([entry[7] for entry in table[1:]], [Decimal('.5'), Decimal('-.25'), Decimal('1')])
        self.assertEqual(table[3][5], Decimal('1e-39'))

    def test_missing_prices_remain_explicit_without_fake_percentages(self):
        rows = [row(entry=None), row('OLDUSDT', '2', close=NOW-1000)]
        data = build_report(rows, {'BTCUSDT': {'price': '130', 'at_ms': NOW}}, NOW)
        table = cells(data)
        self.assertEqual(table[1][6:], ['غير متاح', 'غير محسوب'])
        self.assertEqual(table[2][5], 'غير مسجل')
        self.assertEqual(table[2][7], 'غير محسوب')
        self.assertIn('لا يتم تخمينه', str(cells(data, 'sheet2')))

    def test_formula_like_symbols_are_inline_text_and_illegal_xml_is_removed(self):
        data = build_report([row('=1+1\x00<&USDT')], {}, NOW)
        with ZipFile(BytesIO(data)) as archive:
            tree = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
            self.assertEqual(tree.findall('.//s:f', NS), [])
        self.assertEqual(cells(data)[1][1], '=1+1<&/USDT')

    def test_signal_date_uses_boundary_and_each_dates_dst_offset(self):
        rows = []
        for day in (24, 25):
            close = int(datetime(2026, 10, day, 8, tzinfo=timezone.utc).timestamp()*1000)-1
            rows.append(row(close=close))
        table = cells(build_report(rows, {}, NOW))
        self.assertEqual(table[1][3:5], ['2026-10-24', '11:00'])
        self.assertEqual(table[2][3:5], ['2026-10-25', '10:00'])

    def test_source_qualified_prices_never_cross_same_coin_or_collapse_rows(self):
        rows = [row('ABC_USDT', '2', exchange_name='Gate.io Spot', quote_key=('gate', 'ABC_USDT')),
                row('ABCUSDT', '100', exchange_name='Binance Spot', quote_key=('binance', 'ABCUSDT'))]
        quotes = {('gate', 'ABC_USDT'): {'price': '3', 'at_ms': NOW+1000},
                  ('binance', 'ABCUSDT'): {'price': '125', 'at_ms': NOW+2000}}
        data = build_report(rows, quotes, NOW+3000)
        table = cells(data)
        self.assertEqual(len(table[0]), 8)
        self.assertEqual([entry[1] for entry in table[1:]],
                         ['ABC/USDT', 'ABC/USDT'])
        self.assertEqual([entry[2] for entry in table[1:]], ['Binance', 'Gate.io'])
        self.assertEqual([entry[6:] for entry in table[1:]],
                         [[Decimal('125'), Decimal('.25')], [Decimal('3'), Decimal('.5')]])
        metadata = cells(data, 'sheet2')
        self.assertIn(['ABC/USDT • Binance — السعر الحالي الأصلي', '125'], metadata)
        self.assertIn(['ABC/USDT • Gate.io — السعر الحالي الأصلي', '3'], metadata)
        self.assertIn(['ABC/USDT • Gate.io — وقت جلب السعر UTC', '2026-10-25 (08:00:01)'], metadata)
        self.assertIn(['ABC/USDT • Binance — وقت جلب السعر UTC', '2026-10-25 (08:00:02)'], metadata)
        self.assertNotIn('Spot', str(table) + str(metadata))

    def test_missing_gate_quote_does_not_reuse_binance_price_and_native_unicode_is_preserved(self):
        rows = [row('币安_USDT', '2', exchange_name='Gate.io Spot', quote_key=('gate', '币安_USDT'),
                    chat_id=987654321, telegram_message_id=12345678)]
        quotes = {'币安_USDT': {'price': '999', 'at_ms': NOW},
                  ('binance', '币安_USDT'): {'price': '888', 'at_ms': NOW}}
        data = build_report(rows, quotes, NOW)
        table = cells(data)
        self.assertEqual(table[1][1:3], ['币安/USDT', 'Gate.io'])
        self.assertEqual(table[1][6:], ['غير متاح', 'غير محسوب'])
        all_text = str(table) + str(cells(data, 'sheet2'))
        self.assertNotIn('987654321', all_text)
        self.assertNotIn('12345678', all_text)
        self.assertNotIn('chat_id', all_text)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.stop = threading.Event()
        self.store = Mock()
        self.store.is_admin.side_effect = lambda chat: chat == 100
        self.store.report_rows.return_value = [row(), row(close=NOW-1000), row('ETHUSDT', '2')]
        self.store.get_ledger_started_at_ms.return_value = NOW-10000
        self.client = Mock()
        self.client.latest_price.side_effect = lambda symbol: '120' if symbol == 'BTCUSDT' else '3'
        self.document, self.notice = Mock(), Mock()
        self.service = ReportService(self.store, self.client, self.document, self.notice, self.stop,
                                     clock=lambda: NOW/1000, request_delay=0)
        self.addCleanup(self.shutdown)

    def shutdown(self):
        self.stop.set()
        self.service.join(2)

    def wait_for(self, predicate):
        deadline = time.monotonic()+2
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.005)
        self.fail('Report worker did not finish in time.')

    def test_only_owner_can_request_and_prices_are_fetched_once_per_symbol(self):
        self.assertFalse(self.service.request(200))
        self.assertTrue(self.service.request(100))
        self.wait_for(lambda: self.document.called)
        self.assertEqual(self.client.latest_price.call_count, 2)
        self.assertEqual(self.document.call_args.args[0], 100)
        self.assertTrue(self.document.call_args.args[2].endswith('.xlsx'))
        self.assertEqual(len(cells(self.document.call_args.args[1]))-1, 3)

    def test_request_is_nonblocking_and_duplicate_is_coalesced(self):
        entered, release = threading.Event(), threading.Event()
        def slow(symbol):
            entered.set()
            release.wait(2)
            return '1'
        self.client.latest_price.side_effect = slow
        before = time.monotonic()
        self.assertTrue(self.service.request(100))
        self.assertLess(time.monotonic()-before, .2)
        self.assertTrue(entered.wait(1))
        self.assertFalse(self.service.request(100))
        release.set()
        self.wait_for(lambda: self.document.called)
        self.assertEqual(self.document.call_count, 1)

    def test_empty_ledger_returns_notice_without_market_calls(self):
        self.store.report_rows.return_value = []
        self.service.request(100)
        self.wait_for(lambda: self.notice.called)
        self.document.assert_not_called()
        self.client.latest_price.assert_not_called()
        self.assertIn('لا توجد إشارات مرسلة مسجلة', self.notice.call_args.args[1])

    def test_cooldown_stops_more_market_requests_and_missing_quotes_are_explicit(self):
        self.client.latest_price.side_effect = MarketError('private detail', status=429, retry_after=60)
        with self.assertLogs('reporting', level='WARNING') as log:
            self.service._generate(100)
        self.assertNotIn('private detail', str(log.output))
        self.assertEqual(self.client.latest_price.call_count, 1)
        self.assertTrue(self.document.called)
        table = cells(self.document.call_args.args[1])
        self.assertTrue(all(row[6] == 'غير متاح' for row in table[1:]))

    def test_owner_change_during_generation_prevents_document_delivery(self):
        def transfer_owner(symbol):
            self.store.is_admin.side_effect = lambda chat: False
            return '1'
        self.client.latest_price.side_effect = transfer_owner
        self.service._generate(100)
        self.document.assert_not_called()

    def test_shutdown_interrupts_long_retry_after_wait(self):
        attempted = threading.Event()
        def blocked(*args):
            attempted.set()
            raise AlertDeliveryError('limited', status=429, retry_after=10000)
        self.document.side_effect = blocked
        self.service.request(100)
        self.assertTrue(attempted.wait(1))
        self.shutdown()
        self.assertFalse(self.service.is_alive())
        self.assertEqual(self.document.call_count, 1)

    def test_multisource_service_fetches_once_per_platform_pair(self):
        self.store.report_rows.return_value = [
            row('ABCUSDT', '100', exchange_name='Binance Spot', quote_key=('binance', 'ABCUSDT')),
            row('ABC_USDT', '2', exchange_name='Gate.io Spot', quote_key=('gate', 'ABC_USDT')),
            row('ABC_USDT', '1', close=NOW-1000, exchange_name='Gate.io Spot', quote_key=('gate', 'ABC_USDT'))]
        self.client.latest_price.side_effect = lambda key: '125' if key[0] == 'binance' else '3'
        self.service._generate(100)
        self.assertEqual([call.args[0] for call in self.client.latest_price.call_args_list],
                         [('binance', 'ABCUSDT'), ('gate', 'ABC_USDT')])
        table = cells(self.document.call_args.args[1])
        self.assertEqual(len(table)-1, 3)
        self.assertEqual([entry[7] for entry in table[1:]], [Decimal('2'), Decimal('.25'), Decimal('.5')])

    def test_gate_cooldown_keeps_binance_quote_and_skips_only_remaining_gate_pairs(self):
        self.store.report_rows.return_value = [
            row('ABC_USDT', '2', exchange_name='Gate.io Spot', quote_key=('gate', 'ABC_USDT')),
            row('DEF_USDT', '2', exchange_name='Gate.io Spot', quote_key=('gate', 'DEF_USDT')),
            row('ABCUSDT', '100', exchange_name='Binance Spot', quote_key=('binance', 'ABCUSDT'))]
        def ticker(key):
            if key[0] == 'gate':
                raise MarketError('private diagnostic', status=429, retry_after=120)
            return '125'
        self.client.latest_price.side_effect = ticker
        with self.assertLogs('reporting', level='WARNING') as log:
            self.service._generate(100)
        self.assertNotIn('private diagnostic', str(log.output))
        self.assertEqual(self.client.latest_price.call_count, 2)
        table = cells(self.document.call_args.args[1])
        self.assertEqual(table[1][6:], [Decimal('125'), Decimal('.25')])
        self.assertEqual(table[2][6:], ['غير متاح', 'غير محسوب'])
        self.assertEqual(table[3][6:], ['غير متاح', 'غير محسوب'])

    def test_binance_cooldown_does_not_prevent_later_gate_quote(self):
        self.store.report_rows.return_value = [
            row('ABCUSDT', '100', exchange_name='Binance Spot', quote_key=('binance', 'ABCUSDT')),
            row('ABC_USDT', '2', exchange_name='Gate.io Spot', quote_key=('gate', 'ABC_USDT'))]
        def ticker(key):
            if key[0] == 'binance':
                raise MarketError('private diagnostic', status=429, retry_after=120)
            return '3'
        self.client.latest_price.side_effect = ticker
        with self.assertLogs('reporting', level='WARNING'):
            self.service._generate(100)
        self.assertEqual(self.client.latest_price.call_count, 2)
        table = cells(self.document.call_args.args[1])
        self.assertEqual(table[1][6:], ['غير متاح', 'غير محسوب'])
        self.assertEqual(table[2][6:], [Decimal('3'), Decimal('.5')])

    def test_generation_failure_is_sanitized_and_worker_remains_available(self):
        self.store.report_rows.side_effect = ValueError('secret detail')
        with self.assertLogs('reporting', level='ERROR') as log:
            self.service.request(100)
            self.wait_for(lambda: self.notice.called and not self.service._active)
        self.assertNotIn('secret detail', str(log.output))
        self.assertTrue(self.service.is_alive())
        self.store.report_rows.side_effect = None
        self.service.request(100)
        self.wait_for(lambda: self.document.called)


if __name__ == '__main__':
    unittest.main()
