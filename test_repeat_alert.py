import unittest
from unittest.mock import Mock, patch
from alerts import AlertStore
from bot import send_alert_message
DAY=86400000
class RepeatTests(unittest.TestCase):
 def setUp(self):
  self.s=AlertStore(':memory:');self.addCleanup(self.s.close)
 def receipt(self,close,sent,chat=1,symbol='BTCUSDT',confirmed=True):
  a=self.s.prepare_delivery(chat,symbol,close,'10',sent,'message',sent)
  if confirmed:self.s.confirm_delivery(a,close+100,sent,sent)
 def test_count_deduplicates_and_uses_recipient_and_window(self):
  self.receipt(1,8*DAY);self.receipt(1,9*DAY)
  self.receipt(2,2*DAY);self.receipt(3,9*DAY,chat=2)
  self.receipt(4,9*DAY,symbol='ETHUSDT');self.receipt(5,9*DAY,confirmed=False)
  self.receipt(6,9*DAY)
  self.assertEqual(self.s.repeat_alert_note(1,'BTCUSDT',6,10*DAY),'Last alert: 2 days ago (2/7D) 🔁')
 def test_hours_count_and_no_first_alert_note(self):
  self.assertEqual(self.s.repeat_alert_note(1,'BTCUSDT',10,10*DAY),'')
  for i in range(8):self.receipt(i+1,10*DAY-13*3600000-i*3600000)
  self.assertEqual(self.s.repeat_alert_note(1,'BTCUSDT',10,10*DAY),'Last alert: 13 hours ago (9/7D) 🔁')
 @patch('bot.api')
 @patch('bot.time.time',return_value=10*DAY/1000)
 def test_sent_and_saved_message_include_note(self,clock,api):
  self.receipt(1,8*DAY)
  api.return_value={'message_id':99,'date':10*DAY//1000,'chat':{'id':1}}
  c=Mock();c.latest_price.return_value='10'
  send_alert_message('test',c,1,'📈 BTCUSDT\n🏷️ Binance Spot',store=self.s,signal_close_time=2)
  text=api.call_args.args[2]['text']
  self.assertTrue(text.endswith('Last alert: 2 days ago (2/7D) 🔁'))
  self.assertEqual(self.s.report_rows(1)[-1]['message_text'],text)
