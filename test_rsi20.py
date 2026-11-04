"""Additional RSI20 signals preserve the original RSI30 strategy."""
from dataclasses import asdict
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
from indicators import entry_signal, match_events, Signal, STRATEGY_VERSION, RSI30_STRATEGY_VERSION
from alerts import AlertStore, Scanner, format_alert
from market import Candle, FOUR_HOURS_MS
from history import historical_signals, HistoryService


def candles(n=3):
 return [Candle(i*FOUR_HOURS_MS,(i+1)*FOUR_HOURS_MS-1,10,11,9,10) for i in range(n)]
def evaluate(r,k,d):
 with patch('indicators.rsi',return_value=r),patch('indicators.stochastic',return_value=(k,d)):
  return entry_signal(candles(len(r)))

class Rsi20Tests(unittest.TestCase):
 def test_same_candle_and_adjacent_in_both_orders(self):
  for r,k,d,events in [([18,19,23],[8,9,15],[10,11,13],(2,2)),([19,22,23],[8,9,15],[10,11,13],(1,2)),([18,19,23],[8,15,26],[10,13,18],(2,1))]:
   with self.subTest(r=r,k=k):
    s=evaluate(r,k,d);self.assertIsNotNone(s);self.assertEqual(s.rsi_threshold,20)
    self.assertEqual(s.rsi_event_close_time,candles()[events[0]].close_time)
    self.assertEqual(s.stoch_event_close_time,candles()[events[1]].close_time)
    self.assertLessEqual(s.rsi_event_previous,20);self.assertGreater(s.rsi_event_current,20)
 def test_limits_rising_lines_and_completion_still_required(self):
  for r,k,d in [([18,19,20],[8,9,15],[10,11,13]),([19,22,19],[8,9,15],[10,11,13]),([18,19,23],[8,9,15],[10,14,13]),([18,19,23],[8,15,10],[10,13,18]),([21,22,23],[8,9,15],[10,11,13])]:
   with self.subTest(r=r,k=k,d=d):self.assertIsNone(evaluate(r,k,d))
  self.assertIsNotNone(evaluate([19,20,20.001],[8,9,15],[10,11,13]))
 def test_priority_30_and_legacy_fields(self):
  for r in ([18,19,35],[19,25,35],[19,35,36]):
   s=evaluate(r,[8,9,15],[10,11,13]);self.assertEqual(s.rsi_threshold,30)
   self.assertNotIn('💎',format_alert('BTCUSDT',s))
  s=evaluate([18,19,23],[8,9,15],[10,11,13]);m=format_alert('ABC_USDT',s,exchange_name='Gate.io Spot')
  self.assertIn('📊 RSI (14): 19.00 → 23.00 💎',m)
  self.assertIn('RSI عبور فوق الـ20:',m)
  self.assertEqual(m.count('💎'),1)
  old=asdict(s);old.pop('rsi_threshold');self.assertEqual(Signal(**old).rsi_threshold,30)
 def test_history_one_match_per_completion_and_both_types(self):
  r=[19,23,18,35];k=[9,15,9,15];d=[11,13,11,13];cs=candles(4)
  with patch('indicators.rsi',side_effect=lambda p:r[:len(p)]),patch('indicators.stochastic',side_effect=lambda p:(k[:len(p)],d[:len(p)])):
   found=historical_signals(cs,cs[-1].close_time)
  self.assertEqual([s.rsi_threshold for s in found],[20,30])
  self.assertEqual(len({s.close_time for s in found}),len(found))
 def test_additive_migration_preserves_30_queue_and_boundary(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'alerts.db';s=AlertStore(p);s.subscribe(1,0)
   old=Signal(1000,29,31,10,18,14,16,1000,1000)
   s.record('BTCUSDT',1000,old);s._set_metadata('strategy_version',RSI30_STRATEGY_VERSION);s._set_metadata('strategy_activation_ms',500);s.close()
   with patch('alerts.time.time',return_value=10):s=AlertStore(p)
   self.assertEqual(s.strategy_activation_ms,500);self.assertEqual(s.rsi20_activation_ms,10000)
   self.assertTrue(s.is_pending(1,'BTCUSDT',1000));s.close()
   with patch('alerts.time.time',return_value=20):s=AlertStore(p)
   self.assertEqual(s.rsi20_activation_ms,10000);self.assertTrue(s.is_pending(1,'BTCUSDT',1000));s.close()
 def test_scanner_skips_old20_but_keeps_old30_and_new20(self):
  s=AlertStore(':memory:');self.addCleanup(s.close);s.subscribe(1,0)
  cs=candles(4);s.rsi20_activation_ms=cs[2].close_time+1
  client=Mock();client.server_time.return_value=cs[-1].close_time+16000;client.spot_symbols.return_value=['BTCUSDT'];client.candles.return_value=cs
  send=Mock();scan=Scanner(client,s,send,request_delay=0)
  def detect(p):
   i=len(p)-1;c=p[-1].close_time
   return Signal(c,19,23,9,15,11,13,c,c,rsi_threshold=30 if i==1 else 20)
  with patch('alerts.entry_signal',side_effect=detect):scan.scan_once()
  saved=s._db.execute('SELECT close_time FROM signals ORDER BY close_time').fetchall()
  self.assertEqual(saved,[(cs[1].close_time,),(cs[3].close_time,)])
