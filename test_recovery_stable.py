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
            "HYDROGEL_PACK": OrderDepth({10010: 100}, {10012: -100}),
        }
        positions = {"HYDROGEL_PACK": 15}
        orders, _conv, td = trader.run(self._mk_state(trader_data=seed, order_depths=depths, positions=positions, timestamp=200))
        buys = sum(o.quantity for o in orders.get("HYDROGEL_PACK", []) if o.quantity > 0)
        sells = sum(-o.quantity for o in orders.get("HYDROGEL_PACK", []) if o.quantity < 0)
        self.assertEqual(buys, 0)
        self.assertGreater(sells, 0)
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


if __name__ == "__main__":
    unittest.main()
