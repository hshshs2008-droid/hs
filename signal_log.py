"""Shared confirmed alerts, with private-chat detail replies and no market scans."""
import hashlib
import json
import time
from datetime import datetime
from html import escape
from alerts import DISPLAY_TZ


class SignalLog:
    uses_delivery_log = True
    last_error = refresh_error = None

    def __init__(self, view, stores, send, api, token):
        self.view, self.stores, self.send = view, stores, send
        self.api, self.token = api, token
        self.username = None

    @staticmethod
    def identity(row):
        return (row['quote_key'][0], row['symbol'], row['signal_close_time'], row.get('timeframe', '4h'))

    @classmethod
    def key(cls, row):
        return hashlib.sha256(json.dumps(cls.identity(row)).encode()).hexdigest()[:24]

    def request(self, chat):
        if not self.view.can_access(chat):
            return False
        now = int(time.time()*1000)
        rows = [r for r in self.view.report_rows(chat)
                if r.get('telegram_sent_at_ms') is not None
                and now-48*3600000 <= r['telegram_sent_at_ms'] <= now]
        rows.sort(key=lambda r: (r['telegram_sent_at_ms'], self.identity(r)))
        if not self.username:
            self.username = self.api(self.token, 'getMe', {})['username']
        header = '📊 إشارات آخر 48 ساعة\nمرتبة حسب أول إرسال • الأوقات لإغلاق الشموع\n\n'
        pages, page = [], header
        for row in rows:
            stamp = datetime.fromtimestamp((row['signal_close_time']+1)/1000, DISPLAY_TZ)
            source, symbol, _, frame = self.identity(row)
            label = 'Binance' if source == 'binance' else 'Gate.io'
            link = f'https://t.me/{self.username}?start=sig_{self.key(row)}'
            line = f'<a href="{link}">✅</a> {escape(symbol.replace("_", ""))} | {frame} | {label} | {stamp:%d/%m} | {stamp:%H:%M}\n'
            if len((page+line).encode('utf-16-le'))//2 > 3500:
                pages.append(page); page = header
            page += line
        pages.append(page if rows else header+'لا توجد توصيات مرسلة خلال آخر 48 ساعة.')
        for page in pages:
            if not self.view.can_access(chat):
                return False
            self.send(chat, page, 'HTML')
        return True

    def detail(self, chat, key):
        if not self.view.can_access(chat):
            return
        row = next((r for r in self.view.report_rows(chat) if self.key(r) == key), None)
        if row is None:
            self.send(chat, 'الإشارة غير متاحة في السجل.')
            return
        source, symbol, close, frame = self.identity(row)
        store = self.stores[(source, frame)]
        with store._lock:
            original = store._db.execute('''SELECT telegram_message_id FROM delivery_ledger
                WHERE status='sent' AND chat_id=? AND symbol=? AND signal_close_time=?
                AND telegram_message_id IS NOT NULL ORDER BY telegram_sent_at_ms, attempted_at_ms LIMIT 1''',
                (chat, symbol, close)).fetchone()
            saved = store._db.execute('''SELECT message_text FROM delivery_ledger
                WHERE status='sent' AND symbol=? AND signal_close_time=?
                ORDER BY COALESCE(telegram_sent_at_ms,confirmed_at_ms),attempted_at_ms,attempt_id LIMIT 1''',
                (symbol, close)).fetchone()
        from bot import format_signal_display
        text = format_signal_display(saved[0])
        text = '\n'.join(line for line in text.splitlines() if 'Last alert:' not in line)
        payload = {'chat_id': chat, 'text': '📋 تفاصيل توصية محفوظة\n'+text}
        if original:
            payload['reply_parameters'] = {'message_id': original[0], 'allow_sending_without_reply': True}
        if self.view.can_access(chat):
            self.api(self.token, 'sendMessage', payload)

    def cached_result(self): return None
    def start_background(self): return False
    def is_alive(self): return False
    def join(self, timeout=None): pass
