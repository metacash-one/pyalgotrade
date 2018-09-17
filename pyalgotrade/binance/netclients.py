
from __future__ import print_function

import hmac, hashlib, time
from urllib3.util import parse_url

import requests
import ujson as json
from requests.auth import AuthBase
from pyalgotrade.orderbook import Increase, Decrease, Ask, Bid, Assign, MarketSnapshot

BTCUSD, BTCEUR = 'BTCUSD', 'BTCEUR'

LOCAL_SYMBOL = { BTCUSD: 'BTC-USD', BTCEUR: 'BTC-EUR' }
SYMBOL_LOCAL = { v: k for k, v in LOCAL_SYMBOL.items() }
SYMBOLS = list(LOCAL_SYMBOL.keys())
VENUE = 'binance'


def flmath(n):
    return round(n, 12)

def fees(txnsize):
    return flmath(txnsize * float('0.0025'))


# ---------------------------------------------------------------------------
#  Binance market data message helper / decoder
# ---------------------------------------------------------------------------


def toBookMessages(binance_json, symbol):
    """convert a binance json message into a list of book messages"""
    cbase = binance_json
    if type(cbase) != dict:
        cbase = json.loads(cbase)
    cbt = cbase['type']
    if cbt == 'received':
        return []
    if cbt == 'done' and cbase['order_type'] == 'market':
        return []
    side = { 'buy': Bid, 'sell': Ask }.get(cbase['side'], None)
    if side is None: raise ValueError("Unknown side %r" % cbase['side'])
    if not 'price' in cbase: return [] #change of a market order
    price = cbase['price']
    if cbt == 'done':
        mtype, size = Decrease, cbase['remaining_size']
    elif cbt == 'open':
        mtype, size = Increase, cbase['remaining_size']
    elif cbt == 'match':
        mtype, size = Decrease, cbase['size']
    elif cbt == 'change':
        if price == 'null': return []
        mtype = Decrease
        size = flmath(float(cbase['old_size']) - float(cbase['new_size']))
    else:
        raise ValueError("Unknown binance message: %r" % cbase)
    #rts = datetime.strptime(cbase['time'], "%Y-%m-%dT%H:%M:%S.%fZ")
    rts = int(cbase['sequence'])
    return [mtype(rts, VENUE, symbol, float(price), float(size), side)]


# ---------------------------------------------------------------------------
#  Utilities
# ---------------------------------------------------------------------------


class lazy_init(object):
    """
    A decorator for single, lazy, initialization of (usually) a property
    Could also be viewed as caching the first return value
    """
    def __init__(self, f):
        self.val = None
        self.f = f

    def __call__(self, *args, **kwargs):
        if self.val is None:
            self.val = self.f(*args, **kwargs)
        return self.val



# ---------------------------------------------------------------------------
#  Binance authentication helpers
# ---------------------------------------------------------------------------

class BinanceAuth(AuthBase):
    """a requests-module-compatible auth module"""
    def __init__(self, key, secret):
        self.api_key    = key
        self.secret_key = secret

    def __call__(self, request):
        # all auths need a header set
        request.headers.update({
            'X-MBX-APIKEY': self.api_key
        })
        return request


class BinanceSign(BinanceAuth):

    RECV_WINDOW = 5000

    def __call__(self, request):
        super(BinanceSign, self).__call__(request)

        def signature(message):
            return hmac.new(self.secret_key.encode('utf-8'), message, hashlib.sha256).hexdigest()

        # put the required timestamp into the data
        timestamp = str(int(time.time()*1000))
        if request.method == 'POST':
            print("Got POST")
            request.data = getattr(request, 'data', {})
            request.data['timestamp'] = timestamp
            request.data['recvWindow'] = self.RECV_WINDOW
            request.prepare_body(request.data, [])
            request.data['signature'] = signature(request.body)
            request.prepare_body(request.data, [])

        else:
            request.params = getattr(request, 'params', {})
            request.params['timestamp'] = timestamp
            request.params['recvWindow'] = self.RECV_WINDOW
            request.prepare_url(request.url, request.params)
            scheme, auth, host, port, path, query, fragment = parse_url(request.url)
            request.prepare_url(request.url, { 'signature': signature(query) })
        return request


# ---------------------------------------------------------------------------
#  Binance REST client
# ---------------------------------------------------------------------------

URL = "https://api.binance.com/api/"

from attrdict import AttrDict

BinanceOrder = AttrDict

#BinanceOrder = namedtuple('BinanceOrder', 'id size price done_reason status settled filled_size executed_value product_id fill_fees side created_at done_at')
#BinanceOrder.__new__.__defaults__ = (None,) * len(BinanceOrder._fields)

class BinanceRest(object):

    # Time in Force
    GTC = GOOD_TIL_CANCEL = object()
    IOC = IMMEDIATE_OR_CANCEL = object()
    FOK = FILL_OR_KILL = object()

    GTT = GOOD_TIL_TIME = object()
    POST_ONLY = object()

    def __init__(self, key, secret):
        self.__auth = BinanceAuth(key, secret)
        self.__sign = BinanceSign(key, secret)

    def auth(self): return self.__auth

    @property
    @lazy_init
    def _session(self):
        return requests.Session()

    def _request(self, method, url, **kwargs):
        raise_errors = kwargs.get('raise_errors', True)
        if 'raise_errors' in kwargs: del kwargs['raise_errors']
        result = self._session.request(method, URL + url, **kwargs)
        if raise_errors:
            try:
                result.raise_for_status() # raise if not status == 200
            except Exception:
                print("ERROR: " + method + " " + url + " " + repr(kwargs) + " GOT: " + result.text)
                raise
        return result

    def _auth_request(self, method, url, **kwargs):
        if not 'auth' in kwargs: kwargs['auth'] = self.__auth
        return self._request(method, url, **kwargs)

    def _sign_request(self, method, url, **kwargs):
        if not 'auth' in kwargs: kwargs['auth'] = self.__sign
        return self._request(method, url, **kwargs)

    def _get(self, url, **kwargs): return self._request('GET', url, **kwargs)
    def _getj(self, url, **kwargs): return self._get(url, **kwargs).json()
    def _auth_getj(self, url, **kwargs): return self._auth_request('GET', url, **kwargs).json()
    def _auth_postj(self, url, **kwargs): return self._auth_request('POST', url, **kwargs).json()
    def _auth_delj(self, url, **kwargs): return self._auth_request('DELETE', url, **kwargs).json()
    def _sign_getj(self, url, **kwargs): return self._sign_request('GET', url, **kwargs).json()
    def _sign_postj(self, url, **kwargs): return self._sign_request('POST', url, **kwargs).json()


    #
    # Public endpoints
    #

    # ping

    def server_time(self):
        return self._getj('v1/time')

    def book(self, symbol=BTCUSD, limit=100):
        return self._getj('v1/depth', params={ 'symbol': symbol, 'limit': limit })

    def trades(self, symbol=BTCUSD, limit=100):
        return self._getj('v1/trades', params={ 'symbol': symbol, 'limit': limit })

    # historicalTrades, aggTrades, klines, ticker/24hr, ticker/price, ticker/bookTicker,

    #
    # Account (private endpoints)
    #

    def account(self):
        return self._sign_getj('v3/account')

    def balances(self):
        return { j['asset']: float(j['free']) for j in self.account().get('balances',[]) }

    #
    # Orders (private endpoints)
    #

#    def orders(self, status='all'):
#        return self._auth_getj('orders', params={'status': status})
#
#    def Orders(self, status='all'):
#        return  [ BinanceOrder(**o) for o in self.orders(status) ]
#
#    def order_ids(self, status='all'):
#        return [ o['id'] for o in self.orders(status) ]
#
#    def order_statuses(self):
#        return [ o['status'] for o in self.orders() ]
#
#    def order(self, id):
#        """
#        {
#        "id": "d50ec984-77a8-460a-b958-66f114b0de9b",
#        "size": "3.0",
#        "price": "100.23",
#        "done_reason": "canceled",
#        "status": "done",
#        "settled": true,
#        "filled_size": "1.3",
#        "executed_value": "3.69",
#        "product_id": "BTC-USD",
#        "fill_fees": "0.001",
#        "side": "buy",
#        "created_at": "2014-11-14T06:39:55.189376Z",
#        "done_at": "2014-11-14T06:39:57.605998Z"
#    	}
#        """
#        return self._auth_getj('orders/' + str(id))
#
#    def Order(self, id):
#        return BinanceOrder(**(self.order(id)))
#
#    def fills(self, order_id):
#        params = { 'order_id': order_id }
#        return self._auth_getj('fills', params=params)

    def place_order(self, order):
        params = {
            'type' : order.order_type,
            'side' : order.side,
            'product_id' : order.product,
            'stp' : order.stp,
            'price' : order.price,
            'size' : order.size,
            'time_in_force' : order.time_in_force,
            'cancel_after' : order.cancel_after
            }
        return self._auth_postj('orders', json=params)

    def limitorder(self, side, price, size, symbol=BTCUSD, flags=(), cancel_after=None):
        """Place a limit order"""
        bs = { Bid: "BUY", Ask: "SELL" }[side]
        params = {
            'symbol' : LOCAL_SYMBOL[symbol],
            'side' : bs,
            'type' : 'LIMIT',
            'quantity' : size,
            'price' : price,
            }
        if self.GTT in flags:
            if cancel_after is None: raise ValueError("No cancel time specified")
            params['time_in_force'] = 'GTT'
            params['cancel_after'] = cancel_after
        if self.POST_ONLY in flags: params['post_only'] = True
        elif not self.GTT in flags:
            if self.GTC in flags: params['time_in_force'] = 'GTC'
            elif self.IOC in flags: params['time_in_force'] = 'IOC'
            elif self.FOK in flags: params['time_in_force'] = 'FOK'

        return self._auth_postj('orders', json=params)['id']

    def marketorder(self, side, size, symbol=BTCUSD):
        """Place a market order"""
        bs = { Bid: "buy", Ask: "sell" }[side]
        params = {
            'type' : 'market',
            'side' : bs,
            'product_id' : LOCAL_SYMBOL[symbol],
            'size' : size
            }
        return self._auth_postj('orders', json=params)['id']

    def cancel(self, orderId=None, raise_errors=False):
        url = 'orders'
        if orderId is not None: url += '/' + orderId
        return self._auth_delj(url, raise_errors=raise_errors)

    def book(self, symbol=BTCUSD, level=2, raw=False):
        product = LOCAL_SYMBOL[symbol]
        book = self._get("products/" + product + "/book", params={'level':level})
        if raw: return book.text
        else: return book.json()

    def book_snapshot(self, symbol=BTCUSD):
        book = self.book(symbol)
        def mkassign(ts, price, size, side):
            return Assign(ts, VENUE, symbol, price, size, side)
        rts = book['sequence']
        price = lambda e: float(e[0])
        size = lambda e: float(e[1])
        return MarketSnapshot(time.time(), VENUE, symbol,
            [ mkassign(rts, price(e), size(e), Bid) for e in book['bids'] ] +
            [ mkassign(rts, price(e), size(e), Ask) for e in book['asks'] ]
        )

    def inside_bid_ask(self):
        book = self.book(level=1)
        bid = book['bids'][0][0]
        ask = book['asks'][0][0]
        #log.info("Got inside bid: {} ask: {}".format(bid, ask))
        return bid, ask
