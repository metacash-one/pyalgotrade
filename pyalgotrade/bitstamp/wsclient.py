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

import json
import datetime
import threading
import Queue
#from decimal import Decimal
Decimal=float

from pyalgotrade.websocket import pusher
from pyalgotrade.bitstamp import common

from pyalgotrade.orderbook import Bid, Ask, MarketSnapshot, Assign

VENUE = 'bitstamp'


def get_current_datetime():
    return datetime.datetime.now()

# Bitstamp protocol reference: https://www.bitstamp.net/websocket/


class Trade(pusher.Event):
    """A trade event."""

    def __init__(self, dateTime, eventDict):
        super(Trade, self).__init__(eventDict, True)
        self.__dateTime = dateTime

    def getDateTime(self):
        """Returns the :class:`datetime.datetime` when this event was received."""
        return self.__dateTime

    def getId(self):
        """Returns the trade id."""
        return self.getData()["id"]

    def getPrice(self):
        """Returns the trade price."""
        return self.getData()["price"]

    def getAmount(self):
        """Returns the trade amount."""
        return self.getData()["amount"]

    def isBuy(self):
        """Returns True if the trade was a buy."""
        return self.getData()["type"] == 0

    def isSell(self):
        """Returns True if the trade was a sell."""
        return self.getData()["type"] == 1


def toBookMessages(bitstamp_json, symbol):
    """convert a bitstamp json message into a list of book messages"""
    msg = bitstamp_json
    if type(msg) != type({}):
        msg = json.loads(msg)
    rts = msg.get('timestamp', get_current_datetime())
    result = []
    for side, skey in ((Bid, "bids"), (Ask, "asks")):
        for price, size in msg[skey]:
            result.append(Assign(rts, VENUE, symbol, Decimal(price), Decimal(size), side))
    return result


def bookToSnapshot(bitstamp_json, symbol):
    """convert a bitstamp json book into a MarketSnapshot"""
    ts = get_current_datetime()
    data = toBookMessages(bitstamp_json, symbol)
    return MarketSnapshot(ts, VENUE, symbol, data)


class WebSocketClient(pusher.WebSocketClient):
    PUSHER_APP_KEY = "de504dc5763aeef9ff52"

    # Events
    ON_TRADE = 1
    ON_ORDER_BOOK_UPDATE = 2
    ON_CONNECTED = 3
    ON_DISCONNECTED = 4

    def __init__(self):
        super(WebSocketClient, self).__init__(WebSocketClient.PUSHER_APP_KEY, 5)
        self.__queue = Queue.Queue()

    def getQueue(self):
        return self.__queue

    def onMessage(self, msg):
        # If we can't handle the message, forward it to Pusher WebSocketClient.
        event = msg.get("event")
        if event == "trade":
            self.onTrade(Trade(get_current_datetime(), msg))
        elif event == "data" and msg.get("channel") == "order_book":
            self.onOrderBookUpdate(bookToSnapshot(msg['data'], 'BTCUSD'))
        else:
            super(WebSocketClient, self).onMessage(msg)

    ######################################################################
    # WebSocketClientBase events.

    def onOpened(self):
        pass

    def onClosed(self, code, reason):
        common.logger.info("Closed. Code: %s. Reason: %s." % (code, reason))
        self.__queue.put((WebSocketClient.ON_DISCONNECTED, None))

    def onDisconnectionDetected(self):
        common.logger.warning("Disconnection detected.")
        try:
            self.stopClient()
        except Exception, e:
            common.logger.error("Error stopping websocket client: %s." % (str(e)))
        self.__queue.put((WebSocketClient.ON_DISCONNECTED, None))

    ######################################################################
    # Pusher specific events.

    def onConnectionEstablished(self, event):
        common.logger.info("Connection established.")
        self.subscribeChannel("live_trades")
        self.subscribeChannel("order_book")
        self.__queue.put((WebSocketClient.ON_CONNECTED, None))

    def onError(self, event):
        common.logger.error("Error: %s" % (event))

    def onUnknownEvent(self, event):
        common.logger.warning("Unknown event: %s" % (event))

    ######################################################################
    # Bitstamp specific

    def onTrade(self, trade):
        self.__queue.put((WebSocketClient.ON_TRADE, trade))

    def onOrderBookUpdate(self, orderBookUpdate):
        self.__queue.put((WebSocketClient.ON_ORDER_BOOK_UPDATE, orderBookUpdate))


class WebSocketClientThread(threading.Thread):
    def __init__(self):
        super(WebSocketClientThread, self).__init__()
        self.__wsClient = WebSocketClient()

    def getQueue(self):
        return self.__wsClient.getQueue()

    def start(self):
        self.__wsClient.connect()
        super(WebSocketClientThread, self).start()

    def run(self):
        self.__wsClient.startClient()

    def stop(self):
        try:
            common.logger.info("Stopping websocket client.")
            self.__wsClient.stopClient()
        except Exception, e:
            common.logger.error("Error stopping websocket client: %s." % (str(e)))
