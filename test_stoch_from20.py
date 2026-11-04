import unittest
from indicators import match_events

class StochFrom20Tests(unittest.TestCase):
    def test_any_close_level_and_near_example(self):
        for k,d in [([12,32],[16,24]),([12,18],[16,17]),
                    ([11.7624,24.5375],[13.8887,16.8495])]:
            self.assertEqual(match_events([27.84,46.88],k,d),(1,1))
    def test_adjacent_both_orders_and_retained_conditions(self):
        self.assertEqual(match_events([29,31,32],[8,12,32],[10,16,24]),(1,2))
        self.assertEqual(match_events([28,29,31],[12,32,35],[16,24,27]),(2,1))
        self.assertIsNone(match_events([29,31,29],[8,12,32],[10,16,24]))
        self.assertIsNone(match_events([28,29,31],[12,32,25],[16,24,26]))
    def test_return_below20_invalidates_either_line_but_remaining_below_is_valid(self):
        for k,d in [([12,32,19],[16,24,18]),([12,32,25],[16,24,19])]:
            self.assertIsNone(match_events([28,29,31],k,d))
        self.assertEqual(match_events([28,29,31],[12,18,19],[16,17,18]),(2,1))
    def test_rising_both_and_previous_oversold_still_required(self):
        for k,d in [([12,32],[20,24]),([12,18],[16,15]),([20,32],[19,24])]:
            self.assertIsNone(match_events([29,31],k,d))
