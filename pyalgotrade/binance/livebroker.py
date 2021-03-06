# PyAlgoTrade
#
# Copyright 2011-2015 Gabriel Martin Becedillas Ruiz
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
.. moduleauthor:: Gabriel Martin Becedillas Ruiz <gabriel.becedillas@gmail.com>
"""

import Queue
from datetime import datetime

from .. import broker #, Symbol
from .. import logger as pyalgo_logger
from ..orderbook import Bid, Ask
from .netclients import BinanceRest as httpclient
from . import VENUE

logger = pyalgo_logger.getLogger(__name__)

class LiveBroker(broker.Broker):
    """A live broker.

    :param key: API key.
    :type key: string.
    :param secret: API secret.
    :type secret: string.
    :param passphrase: API passphrase.
    :type passphrase: string.


    .. note::
        * Only limit orders are supported.
        * Orders are automatically set as **goodTillCanceled=True** and  **allOrNone=False**.
        * BUY_TO_COVER orders are mapped to BUY orders.
        * SELL_SHORT orders are mapped to SELL orders.
        * API access permissions should include:

          * Account balance
          * Open orders
          * Buy limit order
          * User transactions
          * Cancel order
          * Sell limit order
    """

    QUEUE_TIMEOUT = 0.01

    def __init__(self, key, secret, feed):
        super(LiveBroker, self).__init__()
        self.__stop = False
        self.__httpClient = self.buildHTTPClient(key, secret)
        self.__tradeMonitor = feed
        self.__cash = 0
        self.__shares = {}
        self.__activeOrders = {}
        self.__userTradeQueue = Queue.Queue()

        self.__symbol = feed.getDefaultInstrument()
        feed.getMatchEvent().subscribe(self.onMatchEvent)
        feed.getChangeEvent().subscribe(self.onChangeEvent)

        self.match_lag = None


    def _registerOrder(self, order):
        assert(order.getId() not in self.__activeOrders)
        assert(order.getId() is not None)
        self.__activeOrders[order.getId()] = order

    def _unregisterOrder(self, order):
        assert(order.getId() in self.__activeOrders)
        assert(order.getId() is not None)
        del self.__activeOrders[order.getId()]

    # Factory method for testing purposes.
    def buildHTTPClient(self, key, secret):
        return httpclient(key, secret)

    def refreshAccountBalance(self):
        """Refreshes quantities of all instruments"""

        self.__stop = True  # Stop running in case of errors.
        balance = self.__httpClient.balances()

        cc1, cc2 = self.__symbol.cc1cc2()

        # Cash aka cc2
        usd = float(balance.get(cc2,0))
        self.__cash = round(usd, 2)

        # Asset aka cc1
        asset = float(balance.get(cc1,0))
        self.__shares = {self.__symbol: asset}

        data = {
                'cc1': cc1, 'cc1_': asset,
                'cc2': cc2, 'cc2_': self.__cash,
               }

        logger.info("Account Balance: {cc2_} {cc2}, {cc1_} {cc1}".format(**data))
        self.__stop = False  # No errors. Keep running.

    def refreshOpenOrders(self):
        self.__stop = True  # Stop running in case of errors.
        logger.info("Retrieving open orders for symbol {!s}.".format(self.__symbol))
        openOrders = self.__httpClient.OpenOrders(symbol=self.__symbol) # if we don't specify a symbol, we get penalized
        for openOrder in openOrders:
            self._registerOrder(openOrder)

        logger.info("%d open order/s found" % (len(openOrders)))
        self.__stop = False  # No errors. Keep running.

    def _startTradeMonitor(self):
        self.__stop = True  # Stop running in case of errors.
        logger.info("Initializing trade monitor.")
        self.__tradeMonitor.start()
        self.__stop = False  # No errors. Keep running.

    def __fees(self, order, match):
        if type(order) == broker.LimitOrder: return 0
        return 0.0025 * match.price * match.size

    def _onUserTrade(self, match):
        oid = match.involves(self.__activeOrders.keys())
        order = self.__activeOrders.get(oid, None)
        if order is not None:
            self.refreshAccountBalance()
            fee = self.__fees(order, match)
            dt = datetime.fromtimestamp(match.time)
            oei = broker.OrderExecutionInfo(match.price, match.size, fee, dt)
            order.addExecutionInfo(oei)
            # order updated, do housekeeping
            if not order.isActive():
                self._unregisterOrder(order)
            # Notify that the order was updated.
            if order.isFilled():
                eventType = broker.OrderEvent.Type.FILLED
            else:
                eventType = broker.OrderEvent.Type.PARTIALLY_FILLED
            self.notifyOrderEvent(broker.OrderEvent(order, eventType, oei))
            return True
        return False

    # BEGIN observer.Subject interface
    def start(self):
        super(LiveBroker, self).start()
        self.refreshAccountBalance()
        self.refreshOpenOrders()
        self._startTradeMonitor()

    def stop(self):
        self.__stop = True
        logger.info("Shutting down trade monitor.")
        self.__tradeMonitor.stop()

    def join(self):
        if self.__tradeMonitor.isAlive():
            self.__tradeMonitor.join()

    def eof(self):
        return self.__stop

    def dispatch(self):
        evented = False
        # Handle a user trade, if any
        try:
            match = self.__userTradeQueue.get(True, LiveBroker.QUEUE_TIMEOUT)
            evented = self._onUserTrade(match)
        except Queue.Empty:
            pass
        return evented

    def applyUpdate(self, order, cborder):
        # presumes order is in 'submitted' state
        if cborder is None or cborder.status == 'rejected':
            self._unregisterOrder(order)
            order.switchState(broker.Order.State.CANCELED)
            self.notifyOrderEvent(broker.OrderEvent(order, broker.OrderEvent.Type.CANCELED, None))
        elif cborder.status == 'open':
            order.switchState(broker.Order.State.ACCEPTED)
            self.notifyOrderEvent(broker.OrderEvent(order, broker.OrderEvent.Type.ACCEPTED, None))
        elif cborder.status == 'done':
            if cborder.done_reason == 'canceled':
                if cborder.filled_size != '0':
                    order.addExecutionInfo(self._CBOrderOEI(cborder))
                order.switchState(broker.Order.State.CANCELED)
                self.notifyOrderEvent(broker.OrderEvent(order, broker.OrderEvent.Type.CANCELED, None))
            elif cborder.done_reason == 'filled':
                order.addExecutionInfo(self._CBOrderOEI(cborder))
                self.notifyOrderEvent(broker.OrderEvent(order, broker.OrderEvent.Type.FILLED, None))
            else:
                raise Exception("Unknown done reason: %r" % cborder)
        else:
            raise Exception("Unknown order status: %r" % cborder)

    def _CBOrderOEI(self, cborder):
        dt = datetime.strptime(cborder.done_at, "%Y-%m-%dT%H:%M:%S.%fZ")
        cbprice = float(cborder.price)
        cbfilled = float(cborder.filled_size)
        cbfees = float(cborder.fill_fees)
        return broker.OrderExecutionInfo(cbprice, cbfilled, cbfees, dt)

    def peekDateTime(self):
        # Return None since this is a realtime subject.
        return None

    # END observer.Subject interface

    def onMatchEvent(self, match):
        self.match_lag = datetime.utcnow() - match.datetime
        if match.involves(self.__activeOrders.keys()):
            self.__userTradeQueue.put(match)

    def onChangeEvent(self, change):
        order_id = change.id
        if order_id not in self.__activeOrders: return
        newstate = change.new_state
        if newstate is None: return
        order = self.__activeOrders[order_id]
        oei = change.oei(order)
        if oei is not None:
            order.addExecutionInfo(oei)
            if newstate != order.getState():
                self.notifyOrderEvent(broker.OrderEvent(order, change.event_type, oei))
                oei = None
        if newstate != order.getState():
            order.switchState(newstate)
        self.notifyOrderEvent(broker.OrderEvent(order, change.event_type, oei))
        if not order.isActive():
            self._unregisterOrder(order)
            self.refreshAccountBalance()

    # BEGIN broker.Broker interface

    def getCash(self, includeShort=True):
        return self.__cash

    def getInstrumentTraits(self, instrument):
        return self.__httpClient.instrumentTraits()[instrument]

    def getShares(self, instrument):
        return self.__shares.get(instrument, 0)

    def getPositions(self):
        return self.__shares

    def getActiveOrders(self, instrument=None):
        return self.__activeOrders.values()

    def submitOrder(self, order):
        if order.isInitial():
            # Override user settings based on Bitstamp limitations.
            order.setAllOrNone(False)
            order.setGoodTillCanceled(True)

            side = Bid if order.isBuy() else Ask
            size = order.getQuantity()
            if order.getType() == order.Type.LIMIT:
                price = order.getLimitPrice()
                flags = (httpclient.GTC,)
                newOrderId = self.__httpClient.limitorder(side, price, size, self.__symbol, flags=flags)
            elif order.getType() == order.Type.MARKET:
                newOrderId = self.__httpClient.marketorder(side, size, self.__symbol)
            else:
                raise Exception(VENUE + " only does LIMIT and MARKET orders")

            order.setSubmitted(newOrderId, datetime.now())
            self._registerOrder(order)
            # Switch from INITIAL -> SUBMITTED
            # IMPORTANT: Do not emit an event for this switch because when using the position interface
            # the order is not yet mapped to the position and Position.onOrderUpdated will get called.
            order.switchState(broker.Order.State.SUBMITTED)
            self.notifyOrderEvent(broker.OrderEvent(order, broker.OrderEvent.Type.SUBMITTED, None))
        else:
            raise Exception("The order was already processed")

    def _createOrder(self, orderType, action, instrument, quantity, price):

        if instrument != self.__symbol:
            raise Exception("Only %r instrument is supported" % self.__symbol)

        action = {
            broker.Order.Action.BUY_TO_COVER: broker.Order.Action.BUY,
            broker.Order.Action.BUY:          broker.Order.Action.BUY,
            broker.Order.Action.SELL_SHORT:   broker.Order.Action.SELL,
            broker.Order.Action.SELL:         broker.Order.Action.SELL
        }.get(action, None)
        if action is None:
            raise Exception("Only BUY/SELL orders are supported")

        instrumentTraits = self.getInstrumentTraits(instrument)
        quantity = instrumentTraits.roundQuantity(quantity)
        price = instrumentTraits.roundPrice(price)
        if orderType == broker.MarketOrder:
            return orderType(action, instrument, quantity, False, instrumentTraits)
        elif orderType == broker.LimitOrder:
            return orderType(action, instrument, price, quantity, instrumentTraits)

    def createMarketOrder(self, action, instrument, quantity, onClose=False):
        return self._createOrder(broker.MarketOrder, action, instrument, quantity, 0.)

    def createLimitOrder(self, action, instrument, limitPrice, quantity):
        return self._createOrder(broker.LimitOrder, action, instrument, quantity, limitPrice)

    def createStopOrder(self, action, instrument, stopPrice, quantity):
        raise Exception("Stop orders are not supported")

    def createStopLimitOrder(self, action, instrument, stopPrice, limitPrice, quantity):
        raise Exception("Stop limit orders are not supported")

    def cancelOrder(self, order):
        activeOrder = self.__activeOrders.get(order.getId())
        if activeOrder is None:
            raise Exception("The order is not active anymore")
        if activeOrder.isFilled():
            raise Exception("Can't cancel order that has already been filled")
        # submit the cancel request
        self.__httpClient.cancelOrder(order)
        # state changes will happen when confirmation is received in onChange

    # END broker.Broker interface
