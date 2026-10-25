"""Owner-only delivery-price reports using standard-library XLSX generation."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from io import BytesIO
import logging
import threading
import time
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from alerts import AlertDeliveryError, DISPLAY_TZ
from market import MarketError


logger = logging.getLogger(__name__)
SHEET_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PACKAGE_REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'
CONTENT_NS = 'http://schemas.openxmlformats.org/package/2006/content-types'
HEADERS = ('مسلسل', 'العملة', 'تاريخ ووقت التوصية',
           'السعر وقت الإشارة (عند الإرسال) USDT', 'السعر الحالي USDT', 'التغير %')
REPORT_ERROR = 'تعذر تجهيز تقرير Excel الآن؛ أعد /report لاحقاً.'
MAX_DOCUMENT_BYTES = 45 * 1024 * 1024


def _tag(name):
    return f'{{{SHEET_NS}}}{name}'


def _text(value):
    # Only XML 1.0 characters; all values use inline strings, never formulas.
    return ''.join(char for char in str(value) if char in '\t\n\r' or
                   0x20 <= ord(char) <= 0xD7FF or 0xE000 <= ord(char) <= 0xFFFD or
                   0x10000 <= ord(char) <= 0x10FFFF)


def _price(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (ValueError, InvalidOperation):
        return None
    return number if number.is_finite() and number > 0 and -40 <= number.adjusted() <= 30 else None


def _label(timestamp, *, utc=False):
    if timestamp is None:
        return 'غير مسجل'
    zone = timezone.utc if utc else DISPLAY_TZ
    return datetime.fromtimestamp(timestamp / 1000, zone).strftime('%Y-%m-%d (%H:%M:%S)')


def _coin(symbol):
    return symbol[:-4] + '/USDT' if symbol.endswith('USDT') else symbol


def _column(index):
    label = ''
    while index:
        index, remainder = divmod(index - 1, 26)
        label = chr(65 + remainder) + label
    return label


def _worksheet(rows, widths, *, freeze=True, autofilter=False):
    root = ET.Element(_tag('worksheet'))
    views = ET.SubElement(root, _tag('sheetViews'))
    view = ET.SubElement(views, _tag('sheetView'), workbookViewId='0', rightToLeft='1')
    if freeze:
        ET.SubElement(view, _tag('pane'), ySplit='1', topLeftCell='A2', activePane='bottomLeft', state='frozen')
    columns = ET.SubElement(root, _tag('cols'))
    for index, width in enumerate(widths, 1):
        ET.SubElement(columns, _tag('col'), min=str(index), max=str(index), width=str(width), customWidth='1')
    data = ET.SubElement(root, _tag('sheetData'))
    for row_number, values in enumerate(rows, 1):
        row = ET.SubElement(data, _tag('row'), r=str(row_number))
        if row_number == 1:
            row.set('ht', '42')
            row.set('customHeight', '1')
        for col_number, (value, style, numeric) in enumerate(values, 1):
            cell = ET.SubElement(row, _tag('c'), r=f'{_column(col_number)}{row_number}', s=str(style))
            if numeric:
                ET.SubElement(cell, _tag('v')).text = str(value)
            else:
                cell.set('t', 'inlineStr')
                text = ET.SubElement(ET.SubElement(cell, _tag('is')), _tag('t'))
                text.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                text.text = _text(value)
    if autofilter and rows:
        ET.SubElement(root, _tag('autoFilter'), ref=f'A1:{_column(len(rows[0]))}{len(rows)}')
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def _styles():
    root = ET.Element(_tag('styleSheet'))
    formats = ET.SubElement(root, _tag('numFmts'), count='2')
    ET.SubElement(formats, _tag('numFmt'), numFmtId='164', formatCode='0.########################################')
    ET.SubElement(formats, _tag('numFmt'), numFmtId='165', formatCode='[Green]+0.00%;[Red]-0.00%;0.00%')
    fonts = ET.SubElement(root, _tag('fonts'), count='2')
    for bold in (False, True):
        font = ET.SubElement(fonts, _tag('font'))
        ET.SubElement(font, _tag('sz'), val='11')
        ET.SubElement(font, _tag('name'), val='Calibri')
        if bold:
            ET.SubElement(font, _tag('b'))
            ET.SubElement(font, _tag('color'), rgb='FFFFFFFF')
    fills = ET.SubElement(root, _tag('fills'), count='3')
    ET.SubElement(ET.SubElement(fills, _tag('fill')), _tag('patternFill'), patternType='none')
    ET.SubElement(ET.SubElement(fills, _tag('fill')), _tag('patternFill'), patternType='gray125')
    fill = ET.SubElement(ET.SubElement(fills, _tag('fill')), _tag('patternFill'), patternType='solid')
    ET.SubElement(fill, _tag('fgColor'), rgb='FF17365D')
    ET.SubElement(fill, _tag('bgColor'), indexed='64')
    borders = ET.SubElement(root, _tag('borders'), count='1')
    border = ET.SubElement(borders, _tag('border'))
    for side in ('left', 'right', 'top', 'bottom', 'diagonal'):
        ET.SubElement(border, _tag(side))
    style_xfs = ET.SubElement(root, _tag('cellStyleXfs'), count='1')
    ET.SubElement(style_xfs, _tag('xf'), numFmtId='0', fontId='0', fillId='0', borderId='0')
    xfs = ET.SubElement(root, _tag('cellXfs'), count='4')
    for style in range(4):
        attributes = {'numFmtId': ('164' if style == 2 else '165' if style == 3 else '0'),
                      'fontId': '1' if style == 1 else '0', 'fillId': '2' if style == 1 else '0',
                      'borderId': '0', 'xfId': '0', 'applyAlignment': '1'}
        if style in (2, 3):
            attributes['applyNumberFormat'] = '1'
        xf = ET.SubElement(xfs, _tag('xf'), attributes)
        ET.SubElement(xf, _tag('alignment'), horizontal='center', vertical='center', wrapText='1')
    styles = ET.SubElement(root, _tag('cellStyles'), count='1')
    ET.SubElement(styles, _tag('cellStyle'), name='Normal', xfId='0', builtinId='0')
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def build_report(rows, quotes, generated_at_ms, ledger_started_at_ms=None):
    """Create a private six-column report of confirmed sends, not filter scans.

    ``quotes`` maps each symbol to ``{'price': decimal-string-or-None, 'at_ms': ms}``.
    Entry prices are exactly those recorded for that recipient's Telegram delivery.
    """
    rows = sorted(rows, key=lambda row: (row['signal_close_time'], row['symbol']))
    main = [[(name, 1, False) for name in HEADERS]]
    missing_entry = missing_current = 0
    for index, row in enumerate(rows, 1):
        symbol = row['symbol']
        entry = _price(row.get('quote_price'))
        current = _price(quotes.get(symbol, {}).get('price'))
        missing_entry += entry is None
        missing_current += current is None
        values = [(index, 0, True), (_coin(symbol), 0, False),
                  (_label(row['signal_close_time'] + 1), 0, False),
                  (entry if entry is not None else 'غير مسجل', 2 if entry is not None else 0, entry is not None),
                  (current if current is not None else 'غير متاح', 2 if current is not None else 0, current is not None)]
        if entry is not None and current is not None:
            with localcontext() as context:
                context.prec = 80
                change = current / entry - 1
            values.append((change, 3, True))
        else:
            values.append(('غير محسوب', 0, False))
        main.append(values)
    metadata = [('البند', 'القيمة'),
                ('وقت إنشاء التقرير', _label(generated_at_ms)),
                ('وقت إنشاء التقرير UTC', _label(generated_at_ms, utc=True)),
                ('عدد الإشارات المرسلة المسجلة', len(rows)),
                ('بدء حفظ أسعار الإرسال', _label(ledger_started_at_ms)),
                ('إشارات بدون سعر إرسال مسجل', missing_entry),
                ('إشارات تعذر جلب سعرها الحالي', missing_current),
                ('نطاق السجل', 'جميع الإشارات المرسلة المسجلة لهذا المستلم، دون حد 48 ساعة.'),
                ('تاريخ ووقت التوصية', 'وقت إغلاق شمعة 4h التي أكملت الإشارة، وفق التوقيت المحلي المضبوط للبوت مع التوقيت الصيفي والشتوي.'),
                ('سعر الإشارة', 'السعر الفعلي الظاهر عند إرسال الرسالة؛ قد يختلف وقت الإرسال عن وقت شمعة الإشارة.'),
                ('نسبة التغير', '(السعر الحالي ÷ السعر وقت الإرسال − 1) × 100؛ ليست نتيجة صفقة منفذة ولا تشمل الرسوم.'),
                ('الأسعار الحالية', 'لقطة سعر واحدة لكل عملة عند تجهيز التقرير؛ أوقات جلبها موضحة أدناه، وليست جميعها في اللحظة نفسها.'),
                ('الأسعار القديمة', 'غير مسجل تعني عدم حفظ السعر الأصلي؛ لا يتم تخمينه أو استبداله بسعر الشمعة.'),
                ('الدقة', 'قد يقرّب Excel الخلايا الرقمية إلى 15 رقماً معنوياً؛ النصوص الأصلية الكاملة محفوظة أدناه.'),
                ('تكرار الإرسال', 'صف واحد لكل توصية تلقاها هذا المستلم؛ عند تكرار التسليم تُستخدم أول رسالة مؤكدة.'),
                ('حدود السجل', 'قد لا تظهر إشارات قديمة حُذفت سجلات إرسالها قبل تفعيل حفظ السجل الدائم.'),
                ('تأكيد الإرسال', 'يعتمد التقرير على تأكيدات Telegram؛ المحاولات غير المؤكدة لا تدخل الحسابات. أول إرسال مؤكد قد يختلف عن أول وصول فعلي إذا فُقد رد Telegram.'),
                ('', ''), ('تفاصيل أسعار الإرسال الأصلية', '')]
    for index, row in enumerate(rows, 1):
        metadata.append((f"{index}. {_coin(row['symbol'])} — سعر الإرسال الأصلي", row.get('quote_price') or 'غير مسجل'))
        metadata.append((f'{index}. وقت جلب سعر الإرسال UTC', _label(row.get('quote_at_ms'), utc=True)))
        metadata.append((f'{index}. وقت رسالة Telegram UTC', _label(row.get('telegram_sent_at_ms'), utc=True)))
    metadata.extend([('', ''), ('لقطات الأسعار الحالية', '')])
    for symbol in sorted({row['symbol'] for row in rows}):
        quote = quotes.get(symbol, {})
        metadata.append((_coin(symbol) + ' — السعر الحالي الأصلي', quote.get('price') or 'غير متاح'))
        metadata.append((_coin(symbol) + ' — وقت جلب السعر UTC', _label(quote.get('at_ms'), utc=True)))
    info = [[(value, 1 if index == 0 else 0, False) for value in values]
            for index, values in enumerate(metadata)]
    content = ET.Element('Types', xmlns=CONTENT_NS)
    ET.SubElement(content, 'Default', Extension='rels', ContentType='application/vnd.openxmlformats-package.relationships+xml')
    ET.SubElement(content, 'Default', Extension='xml', ContentType='application/xml')
    for part, kind in (('/xl/workbook.xml', 'sheet.main'), ('/xl/styles.xml', 'styles'),
                       ('/xl/worksheets/sheet1.xml', 'worksheet'), ('/xl/worksheets/sheet2.xml', 'worksheet')):
        ET.SubElement(content, 'Override', PartName=part,
                      ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.' + kind + '+xml')
    package_rels = ET.Element('Relationships', xmlns=PACKAGE_REL_NS)
    ET.SubElement(package_rels, 'Relationship', Id='rId1', Type=REL_NS + '/officeDocument', Target='xl/workbook.xml')
    workbook = ET.Element(_tag('workbook'))
    sheets = ET.SubElement(workbook, _tag('sheets'))
    for index, name in enumerate(('تقرير الإشارات', 'معلومات التقرير'), 1):
        ET.SubElement(sheets, _tag('sheet'), {'name': name, 'sheetId': str(index), f'{{{REL_NS}}}id': f'rId{index}'})
    relationships = ET.Element('Relationships', xmlns=PACKAGE_REL_NS)
    for index in (1, 2):
        ET.SubElement(relationships, 'Relationship', Id=f'rId{index}', Type=REL_NS + '/worksheet', Target=f'worksheets/sheet{index}.xml')
    ET.SubElement(relationships, 'Relationship', Id='rId3', Type=REL_NS + '/styles', Target='styles.xml')
    output = BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        for filename, element in (('[Content_Types].xml', content), ('_rels/.rels', package_rels),
                                  ('xl/workbook.xml', workbook), ('xl/_rels/workbook.xml.rels', relationships)):
            archive.writestr(filename, ET.tostring(element, encoding='utf-8', xml_declaration=True))
        archive.writestr('xl/styles.xml', _styles())
        archive.writestr('xl/worksheets/sheet1.xml', _worksheet(main, (10, 20, 30, 30, 25, 18), autofilter=True))
        archive.writestr('xl/worksheets/sheet2.xml', _worksheet(info, (46, 110)))
    return output.getvalue()


class ReportService:
    """One owner-only worker; generation never blocks Telegram polling."""

    def __init__(self, store, client, send_document, send_notice, stop_event,
                 *, clock=time.time, request_delay=0.15):
        if request_delay < 0:
            raise ValueError('Report request delay must be nonnegative.')
        self.store, self.client = store, client
        self.send_document, self.send_notice = send_document, send_notice
        self.stop, self.clock, self.request_delay = stop_event, clock, request_delay
        self._condition = threading.Condition()
        self._waiting, self._active = set(), set()
        self._thread = None
        self.last_error = None
        self.heartbeat_ms = 0

    def request(self, chat_id):
        if not self.store.is_admin(chat_id):
            return False
        with self._condition:
            if self.stop.is_set() or chat_id in self._waiting or chat_id in self._active:
                return False
            self._waiting.add(chat_id)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name='excel-reports', daemon=True)
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

    def _send(self, function, chat_id, *args):
        while not self.stop.is_set() and self.store.is_admin(chat_id):
            try:
                function(chat_id, *args)
                return True
            except AlertDeliveryError as error:
                if error.status != 429:
                    raise
                if self.stop.wait(max(1, error.retry_after or 60)):
                    return False
        return False

    def _generate(self, chat_id):
        if not self.store.is_admin(chat_id):
            return
        rows = self.store.report_rows(chat_id)
        if not rows:
            self._send(self.send_notice, chat_id,
                       'لا توجد إشارات مرسلة مسجلة بعد. يبدأ حفظ أسعار الإرسال من تفعيل هذا التحديث؛ لا يمكن استعادة الرسائل القديمة تلقائياً.')
            return
        quotes = {}
        blocked = False
        for symbol in sorted({row['symbol'] for row in rows}):
            if self.stop.is_set() or not self.store.is_admin(chat_id):
                return
            if blocked:
                quotes[symbol] = {'price': None, 'at_ms': None}
                continue
            if self.stop.wait(self.request_delay):
                return
            try:
                price = self.client.latest_price(symbol)
                if _price(price) is None:
                    raise MarketError('Invalid current report price.')
                quotes[symbol] = {'price': price, 'at_ms': int(self.clock() * 1000)}
            except MarketError as error:
                quotes[symbol] = {'price': None, 'at_ms': None}
                if error.status in (418, 429, 403, 451):
                    blocked = True
                logger.warning('Report quote unavailable; HTTP status=%s', error.status)
            self.heartbeat_ms = int(self.clock() * 1000)
        if self.stop.is_set() or not self.store.is_admin(chat_id):
            return
        generated = int(self.clock() * 1000)
        document = build_report(rows, quotes, generated, self.store.get_ledger_started_at_ms())
        if len(document) > MAX_DOCUMENT_BYTES:
            self._send(self.send_notice, chat_id, 'حجم تقرير Excel تجاوز حد الإرسال؛ يلزم تصدير السجل على أجزاء.')
            return
        filename = 'signals-report-' + datetime.fromtimestamp(generated / 1000, timezone.utc).strftime('%Y%m%d-%H%M%S') + '.xlsx'
        caption = ('تقرير الإشارات المرسلة وأسعارها الحالية. النسبة محسوبة من السعر المسجل عند الإرسال. '
                   'الأسعار غير المسجلة أو غير المتاحة موضحة داخل الملف.')
        self._send(self.send_document, chat_id, document, filename, caption)

    def _run(self):
        while not self.stop.is_set():
            with self._condition:
                while not self._waiting and not self.stop.is_set():
                    self._condition.wait(timeout=0.25)
                if self.stop.is_set():
                    return
                chat_id = self._waiting.pop()
                self._active.add(chat_id)
            try:
                self._generate(chat_id)
                self.last_error = None
            except Exception as error:
                self.last_error = REPORT_ERROR
                logger.error('Excel report failed; error_type=%s', type(error).__name__)
                try:
                    self._send(self.send_notice, chat_id, REPORT_ERROR)
                except Exception as notice_error:
                    logger.warning('Excel report failure notice unavailable; error_type=%s', type(notice_error).__name__)
            finally:
                with self._condition:
                    self._active.discard(chat_id)
                    self._condition.notify_all()
