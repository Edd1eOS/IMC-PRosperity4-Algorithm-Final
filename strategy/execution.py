"""
strategy/execution.py - Order generation and execution logic.

Four execution modes (in priority order):
  1. aggressive_take_from_book()    - obvious mispricings on the book
  2. intercept_market_trades()      - same-timestamp market trades we can join/front
  3. passive_quote()                - rest-of-market liquidity provision
  4. hedge_with_velvet()            - delta hedge via VELVET

Strict invariants:
  - Never exceed position limits (hard clip before every order)
  - Never chase: only take when edge >= min_edge
  - Market trade interception uses same-timestamp info, not future
  - All returned orders are list[Order], where Order = (symbol, price, qty)

The Order class is defined here for research use.
In trader_submission.py we use the datamodel Order instead.
"""

from collections import defaultdict
from typing import NamedTuple, Optional

# ── Lightweight Order type for research / backtest ────────────────────────────

class Order(NamedTuple):
    symbol: str
    price:  int
    quantity: int   # positive = buy, negative = sell


# ── Execution ─────────────────────────────────────────────────────────────────

class ExecutionEngine:
    """
    Generates orders given current state and model outputs.
    Stateless between ticks (all state is passed in).
    """

    def __init__(self, config, risk_model):
        self.config = config
        self.risk = risk_model

    # ── 1. Aggressive take from book ─────────────────────────────────────────

    def aggressive_take_from_book(
        self,
        symbol,
        bids,           # list of (price, volume) sorted desc
        asks,           # list of (price, volume) sorted asc
        fair_bid,       # our lower bound FV
        fair_ask,       # our upper bound FV
        position,
    ):
        """
        Return orders to take from book when price is clearly wrong.

        Buy if best_ask < fair_bid - take_edge  (ask is too cheap)
        Sell if best_bid > fair_ask + take_edge (bid is too rich)
        """
        orders = []
        take_edge = self.config.get("take_edge_min", 2.0)

        # Buy sweep
        available_buy = self.risk.clip_qty(symbol, 10000)  # max we can buy
        for price, vol in asks:
            if price > fair_bid - take_edge:
                break  # not cheap enough
            qty = min(vol, available_buy)
            qty = self.risk.clip_qty(symbol, qty)
            if qty > 0:
                orders.append(Order(symbol, price, qty))
                available_buy -= qty
                self.risk.positions[symbol] = self.risk.positions.get(symbol, 0) + qty

        # Sell sweep
        available_sell = abs(self.risk.clip_qty(symbol, -10000))
        for price, vol in bids:
            if price < fair_ask + take_edge:
                break  # not rich enough
            qty = min(vol, available_sell)
            sell_qty = self.risk.clip_qty(symbol, -qty)
            if sell_qty < 0:
                orders.append(Order(symbol, price, sell_qty))
                available_sell += sell_qty
                self.risk.positions[symbol] = self.risk.positions.get(symbol, 0) + sell_qty

        return orders

    # ── 2. Intercept market trades ────────────────────────────────────────────

    def intercept_market_trades(
        self,
        market_trades,       # list of Trade objects visible at this timestamp
        fair_values,         # dict: symbol -> (fair_bid, fair_ask)
        position_snapshot,   # dict: symbol -> current_position (before orders)
        cp_filter=None,      # optional: only intercept trades from/to these cps
        symbol_filter=None,  # optional: only intercept these symbols
        side_filter=None,    # optional: "buy" or "sell" or None
    ):
        """
        At the same timestamp we observe market_trades, we can theoretically
        intercept them by matching the price.

        Strategy:
          - If a Mark is SELLING at a price we consider cheap -> we want to buy too
          - If a Mark is BUYING at a price we consider expensive -> we sell

        Per IMC rules: market_trades at timestamp T are observable at timestamp T.
        We place orders AT the market-trade price (or better for us).

        cp_filter: set of counterparties whose trades we follow
        Returns list of Order.
        """
        orders = []
        take_edge = self.config.get("intercept_min_edge", 1.0)

        seen_symbols = set()  # avoid double-counting in same timestamp

        for trade in market_trades:
            sym    = trade.symbol if hasattr(trade, 'symbol') else trade.get('symbol')
            buyer  = trade.buyer  if hasattr(trade, 'buyer')  else trade.get('buyer')
            seller = trade.seller if hasattr(trade, 'seller') else trade.get('seller')
            price  = trade.price  if hasattr(trade, 'price')  else trade.get('price')
            qty    = trade.quantity if hasattr(trade, 'quantity') else trade.get('quantity', 0)

            if symbol_filter and sym not in symbol_filter:
                continue
            if cp_filter and buyer not in cp_filter and seller not in cp_filter:
                continue
            if sym in seen_symbols:
                continue

            fv_pair = fair_values.get(sym)
            if fv_pair is None:
                continue
            fair_bid, fair_ask = fv_pair

            # Seller is SELLING at `price`. If price < fair_bid - edge -> we also buy
            if side_filter != "sell":
                if price < fair_bid - take_edge:
                    buy_qty = self.risk.clip_qty(sym, qty)
                    if buy_qty > 0:
                        orders.append(Order(sym, int(price), buy_qty))
                        self.risk.positions[sym] = (
                            self.risk.positions.get(sym, 0) + buy_qty
                        )
                        seen_symbols.add(sym)

            # Buyer is BUYING at `price`. If price > fair_ask + edge -> we also sell
            if side_filter != "buy":
                if price > fair_ask + take_edge:
                    sell_qty = self.risk.clip_qty(sym, -qty)
                    if sell_qty < 0:
                        orders.append(Order(sym, int(price), sell_qty))
                        self.risk.positions[sym] = (
                            self.risk.positions.get(sym, 0) + sell_qty
                        )
                        seen_symbols.add(sym)

        return orders

    # ── 3. Passive quote ─────────────────────────────────────────────────────

    def passive_quote(
        self,
        symbol,
        quote_bid,
        quote_ask,
        bid_size,
        ask_size,
        position,
    ):
        """
        Place resting passive quotes.
        Adjusts sizes for position limits before placing.
        """
        orders = []

        if bid_size > 0:
            actual_bid_size = self.risk.clip_qty(symbol, bid_size)
            if actual_bid_size > 0:
                orders.append(Order(symbol, int(quote_bid), actual_bid_size))

        if ask_size > 0:
            actual_ask_size = self.risk.clip_qty(symbol, -ask_size)
            if actual_ask_size < 0:
                orders.append(Order(symbol, int(quote_ask), actual_ask_size))

        return orders

    # ── 4. Hedge with VELVET ──────────────────────────────────────────────────

    def hedge_with_velvet(
        self,
        velvet_bids,
        velvet_asks,
        hedge_qty,       # positive = want to buy, negative = want to sell
        velvet_fv,
        max_slippage=3,  # max price movement from FV we accept
    ):
        """
        Execute a VELVET hedge.
        Only transact at prices within max_slippage of fair value.
        Returns list of Orders on VELVETFRUIT_EXTRACT.
        """
        orders = []
        if hedge_qty == 0:
            return orders

        remaining = abs(hedge_qty)
        side = "buy" if hedge_qty > 0 else "sell"

        if side == "buy":
            for price, vol in velvet_asks:
                if velvet_fv is not None and price > velvet_fv + max_slippage:
                    break
                qty = min(vol, remaining)
                qty = self.risk.clip_qty("VELVETFRUIT_EXTRACT", qty)
                if qty > 0:
                    orders.append(Order("VELVETFRUIT_EXTRACT", int(price), qty))
                    remaining -= qty
                    self.risk.positions["VELVETFRUIT_EXTRACT"] = (
                        self.risk.positions.get("VELVETFRUIT_EXTRACT", 0) + qty
                    )
                if remaining <= 0:
                    break
        else:
            for price, vol in velvet_bids:
                if velvet_fv is not None and price < velvet_fv - max_slippage:
                    break
                qty = min(vol, remaining)
                sell_qty = self.risk.clip_qty("VELVETFRUIT_EXTRACT", -qty)
                if sell_qty < 0:
                    orders.append(Order("VELVETFRUIT_EXTRACT", int(price), sell_qty))
                    remaining += sell_qty
                    self.risk.positions["VELVETFRUIT_EXTRACT"] = (
                        self.risk.positions.get("VELVETFRUIT_EXTRACT", 0) + sell_qty
                    )
                if remaining <= 0:
                    break

        return orders

    # ── Helper: parse order book ──────────────────────────────────────────────

    @staticmethod
    def parse_order_depth(order_depth):
        """
        Convert OrderDepth to sorted (price, vol) lists.
        Works with both datamodel OrderDepth and dict-style depth.

        Returns (bids_desc, asks_asc) where each is list of (price, volume).
        """
        if hasattr(order_depth, 'buy_orders'):
            bids = sorted(order_depth.buy_orders.items(),  reverse=True)
            asks = sorted(order_depth.sell_orders.items())
        elif isinstance(order_depth, dict):
            bids = sorted(order_depth.get("bids", {}).items(), reverse=True)
            asks = sorted(order_depth.get("asks", {}).items())
        else:
            bids, asks = [], []

        # Normalize: volumes are always positive
        bids = [(p, abs(v)) for p, v in bids if v != 0]
        asks = [(p, abs(v)) for p, v in asks if v != 0]
        return bids, asks

    @staticmethod
    def best_bid_ask(order_depth):
        bids, asks = ExecutionEngine.parse_order_depth(order_depth)
        best_bid = bids[0][0] if bids else None
        best_ask = asks[0][0] if asks else None
        return best_bid, best_ask

    @staticmethod
    def mid_price(order_depth):
        bb, ba = ExecutionEngine.best_bid_ask(order_depth)
        if bb and ba:
            return (bb + ba) / 2
        return bb or ba
