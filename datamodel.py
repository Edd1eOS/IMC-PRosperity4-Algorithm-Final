"""
Minimal datamodel stub — mirrors IMC Prosperity's built-in datamodel.
Only used for local `import` resolution / syntax checks.
On the IMC cloud sandbox, the platform supplies the real datamodel.
"""

from typing import Dict, List


class Order:
    def __init__(self, symbol: str, price: int, quantity: int) -> None:
        self.symbol   = symbol
        self.price    = price
        self.quantity = quantity

    def __repr__(self) -> str:
        return "Order(" + self.symbol + "," + str(self.price) + "," + str(self.quantity) + ")"


class OrderDepth:
    def __init__(self) -> None:
        self.buy_orders:  Dict[int, int] = {}
        self.sell_orders: Dict[int, int] = {}


class Listing:
    def __init__(self, symbol: str, product: str, denomination: str) -> None:
        self.symbol       = symbol
        self.product      = product
        self.denomination = denomination


class Trade:
    def __init__(self, symbol: str, price: int, quantity: int,
                 buyer: str = "", seller: str = "", timestamp: int = 0) -> None:
        self.symbol    = symbol
        self.price     = price
        self.quantity  = quantity
        self.buyer     = buyer
        self.seller    = seller
        self.timestamp = timestamp


class Observation:
    def __init__(self, plainValueObservations=None, conversionObservations=None) -> None:
        self.plainValueObservations  = plainValueObservations or {}
        self.conversionObservations  = conversionObservations or {}


class TradingState:
    def __init__(
        self,
        traderData: str,
        timestamp: int,
        listings: Dict[str, Listing],
        order_depths: Dict[str, OrderDepth],
        own_trades: Dict[str, List[Trade]],
        market_trades: Dict[str, List[Trade]],
        position: Dict[str, int],
        observations: Observation,
    ) -> None:
        self.traderData    = traderData
        self.timestamp     = timestamp
        self.listings      = listings
        self.order_depths  = order_depths
        self.own_trades    = own_trades
        self.market_trades = market_trades
        self.position      = position
        self.observations  = observations
