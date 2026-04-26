import json
import unittest
from collections import namedtuple

from trader_submission import Trader, _mid
from backtest_runner import _build_order_depth, PnLTracker


OrderDepth = namedtuple("OrderDepth", ["buy_orders", "sell_orders"])


class TestRecoveryStable(unittest.TestCase):
    def _mk_state(self, trader_data="", positions=None, order_depths=None, market_trades=None, own_trades=None, timestamp=100):
        class MockState:
            pass

        s = MockState()
        s.timestamp = timestamp
        s.traderData = trader_data
        s.position = positions or {}
        s.order_depths = order_depths or {}
        s.market_trades = market_trades or {}
        s.own_trades = own_trades or {}
        s.listings = {}
        s.observations = None
        return s

    def test_mid_zero_bid(self):
        od = OrderDepth(buy_orders={0: 10}, sell_orders={1: -10})
        self.assertEqual(_mid(od), 0.5)

    def test_traderdata_persistence(self):
        trader = Trader()
        seed = json.dumps({"ph": "POST_CHOICE_CALL", "sc": {"6000": 7}, "pc": 0.9, "md": "PROFIT_LOCK"})
        state = self._mk_state(trader_data=seed)
        _orders, _conv, td = trader.run(state)
        parsed = json.loads(td)
        self.assertEqual(parsed.get("ph"), "POST_CHOICE_CALL")
        self.assertGreaterEqual(int(parsed.get("sc", {}).get("6000", 0)), 7)

        state2 = self._mk_state(trader_data=td)
        _orders2, _conv2, td2 = trader.run(state2)
        parsed2 = json.loads(td2)
        self.assertEqual(parsed2.get("ph"), "POST_CHOICE_CALL")

    def test_no_vev5400_default(self):
        trader = Trader()
        depths = {
            "VEV_5400": OrderDepth({}, {10: -999}),
        }
        orders, _conv, _td = trader.run(self._mk_state(order_depths=depths))
        self.assertNotIn("VEV_5400", orders)

    def test_vev5500_gated(self):
        trader = Trader()
        depths = {
            "VEV_5500": OrderDepth({}, {8: -999}),
        }
        orders, _conv, _td = trader.run(self._mk_state(order_depths=depths))
        self.assertNotIn("VEV_5500", orders)

    def test_high_strike_lottery(self):
        trader = Trader()
        depths = {
            "VEV_6000": OrderDepth({0: 100}, {1: -999}),
            "VEV_6500": OrderDepth({0: 100}, {1: -999}),
        }
        orders, _conv, _td = trader.run(self._mk_state(order_depths=depths))
        qty_6000 = sum(o.quantity for o in orders.get("VEV_6000", []) if o.quantity > 0)
        qty_6500 = sum(o.quantity for o in orders.get("VEV_6500", []) if o.quantity > 0)
        self.assertLessEqual(qty_6000, 10)
        self.assertLessEqual(qty_6500, 10)
        self.assertGreater(qty_6000, 0)
        self.assertGreater(qty_6500, 0)

    def test_no_pre_choice_delta_hedge(self):
        trader = Trader()
        depths = {
            "VELVETFRUIT_EXTRACT": OrderDepth({5200: 20}, {5201: -20}),
            "VEV_6000": OrderDepth({0: 50}, {1: -50}),
            "VEV_6500": OrderDepth({0: 50}, {1: -50}),
        }
        positions = {"VEV_6000": 200, "VEV_6500": 200, "VELVETFRUIT_EXTRACT": 0}
        orders, _conv, _td = trader.run(self._mk_state(order_depths=depths, positions=positions))
        self.assertEqual(orders.get("VELVETFRUIT_EXTRACT", []), [])

    def test_velvet_disabled(self):
        trader = Trader()
        depths = {
            "VELVETFRUIT_EXTRACT": OrderDepth({5200: 100}, {5201: -100}),
        }
        orders, _conv, td = trader.run(self._mk_state(order_depths=depths, timestamp=1000))
        self.assertEqual(orders.get("VELVETFRUIT_EXTRACT", []), [])
        self.assertEqual(json.loads(td).get("md"), "NORMAL")

    def test_hydrogel_cap_normal(self):
        trader = Trader()
        depths = {
            "HYDROGEL_PACK": OrderDepth({9998: 50}, {10000: -50}),
        }
        positions = {"HYDROGEL_PACK": 29}
        orders, _conv, _td = trader.run(self._mk_state(order_depths=depths, positions=positions))
        buy_qty = sum(o.quantity for o in orders.get("HYDROGEL_PACK", []) if o.quantity > 0)
        self.assertLessEqual(positions["HYDROGEL_PACK"] + buy_qty, 30)

    def test_hydrogel_profit_lock(self):
        trader = Trader()
        seed = json.dumps({
            "cb": {"HYDROGEL_PACK": -99800.0},
            "hp": 700.0,
            "pp": 700.0,
            "lp": {"HYDROGEL_PACK": 15}
        })
        depths = {
            # spread >= 14 so quotes can be placed, but PROFIT_LOCK should prevent buys
            "HYDROGEL_PACK": OrderDepth({9990: 100}, {10010: -100}),
        }
        positions = {"HYDROGEL_PACK": 15}
        orders, _conv, td = trader.run(self._mk_state(trader_data=seed, order_depths=depths, positions=positions, timestamp=200))
        buys = sum(o.quantity for o in orders.get("HYDROGEL_PACK", []) if o.quantity > 0)
        # In PROFIT_LOCK mode, cap = hydrogel_profit_lock_cap = 10, pos = 15 > cap -> no buys
        self.assertEqual(buys, 0)
        self.assertEqual(json.loads(td).get("md"), "PROFIT_LOCK")

    def test_endgame_flat(self):
        trader = Trader()
        depths = {
            "HYDROGEL_PACK": OrderDepth({10000: 50}, {10002: -50}),
        }
        positions = {"HYDROGEL_PACK": 8}
        orders, _conv, td = trader.run(self._mk_state(order_depths=depths, positions=positions, timestamp=960000))
        buys = sum(o.quantity for o in orders.get("HYDROGEL_PACK", []) if o.quantity > 0)
        sells = sum(-o.quantity for o in orders.get("HYDROGEL_PACK", []) if o.quantity < 0)
        self.assertEqual(buys, 0)
        self.assertGreater(sells, 0)
        self.assertEqual(json.loads(td).get("md"), "ENDGAME_FLAT")

    def test_liquidation_mark(self):
        trader = Trader()
        od = OrderDepth({0: 10}, {1: -10})
        self.assertEqual(trader._mark_position("VEV_6000", 10, od), 0)

    def test_no_truthiness_price_bug_in_backtester(self):
        od = _build_order_depth({"bid": 0, "bid_vol": 10, "ask": 1, "ask_vol": 20})
        self.assertIn(0, od.buy_orders)
        self.assertIn(1, od.sell_orders)

        pnl = PnLTracker()
        pnl.positions["VEV_6000"] = 5
        mtm = pnl.mark_to_market({"VEV_6000": 0}, day=1)
        self.assertEqual(mtm, pnl.cash)

    # ── v3 tests ──────────────────────────────────────────────────────────────

    def test_vev6000_base_cap_100(self):
        # Without confirmation, VEV_6000 and VEV_6500 must not exceed 100 each
        trader = Trader()
        depths = {
            "VEV_6000": OrderDepth({0: 200}, {1: -200}),
            "VEV_6500": OrderDepth({0: 200}, {1: -200}),
        }
        orders, _conv, _td = trader.run(self._mk_state(order_depths=depths, timestamp=0))
        qty_6000 = sum(o.quantity for o in orders.get("VEV_6000", []) if o.quantity > 0)
        qty_6500 = sum(o.quantity for o in orders.get("VEV_6500", []) if o.quantity > 0)
        self.assertLessEqual(qty_6000, 100)
        self.assertLessEqual(qty_6500, 100)

    def test_mark01_seller_no_confirmation(self):
        # If Mark 01 is seller, confirmed cap must not activate
        trader = Trader()
        Trade = namedtuple("Trade", ["buyer", "seller", "price", "quantity"])
        depths = {
            "VEV_6000": OrderDepth({0: 200}, {1: -200}),
            "VEV_6500": OrderDepth({0: 200}, {1: -200}),
        }
        mtrades = {
            "VEV_6000": [Trade(buyer="", seller="Mark_01", price=1, quantity=10)],
        }
        orders, _conv, td = trader.run(self._mk_state(order_depths=depths, market_trades=mtrades, timestamp=0))
        state_data = json.loads(td)
        self.assertFalse(bool(state_data.get("vc", 0)))

    def test_mark01_buyer_activates_confirmation(self):
        # If Mark 01 is buyer, confirmed cap can activate next tick
        trader = Trader()
        Trade = namedtuple("Trade", ["buyer", "seller", "price", "quantity"])
        depths = {
            "VEV_6000": OrderDepth({0: 200}, {1: -200}),
            "VEV_6500": OrderDepth({0: 200}, {1: -200}),
        }
        mtrades = {
            "VEV_6000": [Trade(buyer="Mark_01", seller="", price=1, quantity=10)],
        }
        _orders, _conv, td = trader.run(self._mk_state(order_depths=depths, market_trades=mtrades, timestamp=0))
        state_data = json.loads(td)
        self.assertTrue(bool(state_data.get("vc", 0)))

    def test_trade_price_ge2_activates_confirmation(self):
        # If a market trade price >= 2, confirmed cap should activate
        trader = Trader()
        Trade = namedtuple("Trade", ["buyer", "seller", "price", "quantity"])
        depths = {
            "VEV_6000": OrderDepth({1: 200}, {2: -200}),
        }
        mtrades = {
            "VEV_6000": [Trade(buyer="A", seller="B", price=2, quantity=5)],
        }
        _orders, _conv, td = trader.run(self._mk_state(order_depths=depths, market_trades=mtrades, timestamp=0))
        state_data = json.loads(td)
        self.assertTrue(bool(state_data.get("vc", 0)))

    def test_endgame_ts_at_85000(self):
        # endgame_ts must trigger at 85000, not 850000
        trader = Trader()
        depths = {
            "HYDROGEL_PACK": OrderDepth({9998: 50}, {10012: -50}),
        }
        positions = {"HYDROGEL_PACK": 5}
        _orders, _conv, td = trader.run(self._mk_state(order_depths=depths, positions=positions, timestamp=85001))
        self.assertIn(json.loads(td).get("md"), ("PROFIT_LOCK", "RISK_OFF", "ENDGAME_FLAT"))

    def test_final_flat_ts_at_95000(self):
        # final_flat_ts must trigger at 95000
        trader = Trader()
        depths = {
            "HYDROGEL_PACK": OrderDepth({10000: 50}, {10002: -50}),
        }
        positions = {"HYDROGEL_PACK": 5}
        orders, _conv, td = trader.run(self._mk_state(order_depths=depths, positions=positions, timestamp=95001))
        self.assertEqual(json.loads(td).get("md"), "ENDGAME_FLAT")
        buys = sum(o.quantity for o in orders.get("HYDROGEL_PACK", []) if o.quantity > 0)
        self.assertEqual(buys, 0)

    def test_hydrogel_no_trade_narrow_spread(self):
        # HYDROGEL must not trade if spread < 14
        trader = Trader()
        depths = {
            "HYDROGEL_PACK": OrderDepth({9994: 50}, {10000: -50}),  # spread = 6
        }
        orders, _conv, _td = trader.run(self._mk_state(order_depths=depths, timestamp=1000))
        self.assertEqual(orders.get("HYDROGEL_PACK", []), [])

    def test_hydrogel_cap_zero_after_95000(self):
        # HYDROGEL cap must be 0 after 95000
        trader = Trader()
        depths = {
            "HYDROGEL_PACK": OrderDepth({9980: 50}, {10000: -50}),  # spread = 20
        }
        positions = {"HYDROGEL_PACK": 0}
        orders, _conv, _td = trader.run(self._mk_state(order_depths=depths, positions=positions, timestamp=95001))
        buys = sum(o.quantity for o in orders.get("HYDROGEL_PACK", []) if o.quantity > 0)
        self.assertEqual(buys, 0)

    def test_velvet_produces_zero_orders(self):
        # VELVET must always produce zero orders (enable_velvet_alpha=False)
        trader = Trader()
        depths = {
            "VELVETFRUIT_EXTRACT": OrderDepth({5195: 100}, {5200: -100}),
        }
        for ts in [0, 1000, 50000, 80000]:
            orders, _conv, _td = trader.run(self._mk_state(order_depths=depths, timestamp=ts))
            self.assertEqual(orders.get("VELVETFRUIT_EXTRACT", []), [],
                             msg=f"VELVET orders at ts={ts}")


if __name__ == "__main__":
    unittest.main()
