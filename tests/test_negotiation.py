"""Negotiation rules (negotiation.py). Pure logic, no server needed."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import negotiation as n   # noqa: E402

BOARD, MAX = 2000, 2600


class TestNegotiation(unittest.TestCase):

    def new(self, board=BOARD, max_rate=MAX):
        return n.new_state(board, max_rate)

    def test_opening_offer_is_the_board_rate(self):
        self.assertEqual(n.decide(self.new()), ("offer", BOARD))

    def test_yes_accepts_our_current_offer(self):
        s = self.new()
        n.decide(s)
        self.assertEqual(n.decide(s, n.ACCEPTED), ("accept", BOARD))

    def test_counters_climb_toward_max_over_three_rounds(self):
        s = self.new()
        n.decide(s)
        self.assertEqual(n.decide(s, 3000), ("counter", 2200))       # 1/3 of the way
        self.assertEqual(n.decide(s, 3000), ("counter", 2400))       # 2/3 of the way
        self.assertEqual(n.decide(s, 3000), ("final_offer", 2580))   # $20 under max

    def test_never_offers_or_accepts_above_max(self):
        s = self.new()
        n.decide(s)
        offers = [n.decide(s, 9000)[1] for _ in range(3)]
        self.assertTrue(all(o <= MAX for o in offers))
        self.assertEqual(n.decide(s, 9000), ("end", None))           # no deal over max

    def test_after_final_offer_accepts_an_ask_up_to_max(self):
        s = self.new()
        n.decide(s)
        for _ in range(3):
            n.decide(s, 9000)
        self.assertEqual(n.decide(s, MAX), ("accept", MAX))

    def test_ask_at_or_below_our_offer_is_accepted_at_their_number(self):
        s = self.new()
        n.decide(s)
        self.assertEqual(n.decide(s, 1950), ("accept", 1950))

    def test_ask_below_next_counter_is_accepted(self):
        s = self.new()
        n.decide(s)
        self.assertEqual(n.decide(s, 2100), ("accept", 2100))        # under the 2200 counter

    def test_misheard_low_number_asks_to_confirm(self):
        s = self.new()
        n.decide(s)
        self.assertEqual(n.decide(s, 21), ("confirm_rate", None))

    def test_plain_no_moves_to_next_round(self):
        s = self.new()
        n.decide(s)
        self.assertEqual(n.decide(s, n.REJECTED), ("counter", 2200))

    def test_board_at_or_above_max_offers_only_the_max(self):
        s = self.new(board=2800, max_rate=2600)
        self.assertEqual(n.decide(s), ("offer", MAX))
        self.assertEqual(n.decide(s, 3000), ("hold", MAX))
        self.assertEqual(n.decide(s, 3000), ("final_offer", MAX))
        self.assertEqual(n.decide(s, 3000), ("end", None))

    def test_agreed_deal_stays_agreed(self):
        s = self.new()
        n.decide(s)
        n.decide(s, n.ACCEPTED)
        self.assertEqual(n.decide(s, 9000), ("accept", BOARD))


if __name__ == "__main__":
    unittest.main()
