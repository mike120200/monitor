from __future__ import annotations

import math
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

from .core import Http, iso, read_json, write_json


def wilder(values, period=14):
    if len(values) < period:
        raise ValueError('指标预热数据不足')
    avg = sum(values[:period]) / period
    for value in values[period:]:
        avg = (avg * (period - 1) + value) / period
    return avg


def rsi(closes):
    diffs = [b - a for a, b in zip(closes, closes[1:])]
    gain = wilder([max(d, 0) for d in diffs])
    loss = wilder([max(-d, 0) for d in diffs])
    return 50.0 if gain == loss == 0 else 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)


def indicators(rows):
    closes = [r['close'] for r in rows]
    trs = [max(b['high'] - b['low'], abs(b['high'] - a['close']), abs(b['low'] - a['close']))
           for a, b in zip(rows, rows[1:])]
    sma = lambda n, end=None: statistics.fmean(closes[:end][-n:])
    atr = wilder(trs)
    pivots = {'highs': [], 'lows': []}
    for i in range(3, len(rows) - 3):
        neighbors = rows[i-3:i] + rows[i+1:i+4]
        for side, field, op in [('highs', 'high', max), ('lows', 'low', min)]:
            if rows[i][field] == op([r[field] for r in neighbors] + [rows[i][field]]):
                pivots[side].append({'price': rows[i][field], 'time': rows[i]['open_ms']})
    return {'close': closes[-1], 'close_ms': rows[-1]['close_ms'], 'ma20': sma(20),
            'ma20_prev': sma(20, -1), 'ma200': sma(200) if len(rows) >= 200 else None,
            'ma200_prev': sma(200, -1) if len(rows) >= 201 else None,
            'rsi14': rsi(closes), 'rsi14_prev': rsi(closes[:-1]),
            'atr14': atr, 'atr_pct': atr / closes[-1] * 100,
            'distance_ma20_pct': (closes[-1] / sma(20) - 1) * 100,
            'previous_close': closes[-2], 'pivots': {k: v[-6:] for k, v in pivots.items()},
            'seed_open_ms': rows[0]['open_ms'], 'seed_method': '14 observations SMA then Wilder',
            'bars': len(rows), 'recent_bars': rows[-12:]}


class Market:
    def __init__(self, cfg):
        self.cfg, self.http = cfg, Http(cfg)
        self.base = cfg.get('BINANCE_BASE_URL', 'https://api.binance.com').rstrip('/')

    def get(self, route, **params):
        return self.http.request(self.base + route + '?' + urlencode(params))

    def candles(self, interval, now_ms):
        path = self.cfg.runtime / 'cache' / (interval + '.json')
        rows = read_json(path, [])
        span = {'1h': 3600000, '1d': 86400000, '1w': 604800000}[interval]
        if rows and now_ms <= rows[-1]['close_ms'] + span + 2000:
            return rows
        params = dict(symbol='BTCUSDT', interval=interval, limit=1000 if rows else 500)
        if rows:
            params['startTime'] = rows[-1]['open_ms']
        merged = {r['open_ms']: r for r in rows}
        for _ in range(30):
            raw = self.get('/api/v3/klines', **params)
            if not isinstance(raw, list) or not raw:
                raise ValueError('K线返回为空')
            for r in raw:
                if int(r[6]) >= now_ms:
                    continue
                c = {'open_ms': int(r[0]), 'close_ms': int(r[6]),
                     **dict(zip(['open', 'high', 'low', 'close', 'volume'], map(float, r[1:6])))}
                if not all(math.isfinite(c[k]) and c[k] > 0 for k in ['open', 'high', 'low', 'close']):
                    raise ValueError('K线价格无效')
                if not math.isfinite(c['volume']) or c['volume'] < 0 or not (
                    c['low'] <= min(c['open'], c['close']) <= max(c['open'], c['close']) <= c['high']):
                    raise ValueError('K线OHLCV无效')
                merged[c['open_ms']] = c
            if len(raw) < params['limit'] or int(raw[-1][6]) >= now_ms:
                break
            params['startTime'] = int(raw[-1][0]) + span
        rows = sorted(merged.values(), key=lambda r: r['open_ms'])
        if len(rows) < (250 if interval == '1d' else 60):
            raise ValueError('历史K线不足')
        if any(b['open_ms'] - a['open_ms'] != span for a, b in zip(rows, rows[1:])):
            raise ValueError('历史K线不连续，请检查缓存')
        if now_ms - rows[-1]['close_ms'] > span + 180000:
            raise ValueError('K线过期')
        # 保留初始种子，不滚动截断Wilder历史，确保重启一致。
        write_json(path, rows)
        return rows

    def rolling(self, window):
        d = self.get('/api/v3/ticker', symbol='BTCUSDT', windowSize=window, type='FULL')
        result = {k: float(d[k]) for k in ['priceChangePercent', 'lastPrice', 'openPrice']}
        result.update(open_ms=int(d['openTime']), close_ms=int(d['closeTime']))
        age = time.time() - result['close_ms'] / 1000
        span = {'15m': 900, '1h': 3600, '1d': 86400, '7d': 604800}[window]
        actual = (result['close_ms'] - result['open_ms']) / 1000
        if not -30 <= age <= self.cfg.number('MAX_SOURCE_AGE_SECONDS', 180):
            raise ValueError('滚动行情时间过期')
        if not span - 1 <= actual <= span + 60:
            raise ValueError('滚动窗口长度异常')
        if not all(math.isfinite(x) for x in result.values()) or result['lastPrice'] <= 0:
            raise ValueError('滚动行情数值异常')
        return result

    def auxiliary(self):
        result, missing = {}, []
        requests = {
            'fear_greed': 'https://api.alternative.me/fng/?limit=7',
            'coinbase_spot': 'https://api.coinbase.com/v2/prices/BTC-USD/spot',
            'funding': 'https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT',
            'open_interest': 'https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT',
        }
        def fetch(item):
            k, url = item
            cache = self.cfg.runtime / 'cache' / (k + '.json')
            saved = read_json(cache)
            if k == 'fear_greed' and saved and time.time() - saved['fetched_at'] < 3600:
                return k, saved['data']
            d = self.http.request(url)
            write_json(cache, {'fetched_at': time.time(), 'data': d})
            return k, d
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = {k: pool.submit(fetch, (k, url)) for k, url in requests.items()}
            for k, f in jobs.items():
                try:
                    _, result[k] = f.result()
                except Exception:
                    missing.append(k)
        result['limitations'] = ['ETF资金流、美元、美债及新闻由Codex每次联网核实；未核实不得当作最新事实。',
                                '资金费率仅为快照，未统一结算周期或获取30天分布，不据此声称历史分位。',
                                'Coinbase为USD、币安为USDT；价差可能包含稳定币基差。']
        result['missing'] = missing
        return result

    def collect(self):
        started = time.time()
        with ThreadPoolExecutor(max_workers=8) as pool:
            rolls = {w: pool.submit(self.rolling, w) for w in ['15m', '1h', '1d', '7d']}
            candles = {p: pool.submit(self.candles, p, int(started * 1000)) for p in ['1h', '1d', '1w']}
            aux = pool.submit(self.auxiliary)
            rolling = {w: f.result() for w, f in rolls.items()}
            rows = {p: f.result() for p, f in candles.items()}
            background = aux.result()
        price = rolling['15m']['lastPrice']
        if max(x['lastPrice'] for x in rolling.values()) / min(x['lastPrice'] for x in rolling.values()) > 1.01:
            raise ValueError('不同滚动窗口最新价偏差超过1%，本轮不生成建议')
        high = max(r['high'] for r in rows['1d'][-90:])
        daily, weekly = indicators(rows['1d']), indicators(rows['1w'])
        h = rows['1h']
        meanvol = statistics.fmean(r['volume'] for r in h[-21:-1])
        snapshot = {'started_at': started, 'collected_at': time.time(), 'time_utc': iso(),
                    'source': self.base, 'symbol': 'BTCUSDT', 'price': price, 'rolling': rolling,
                    'high90': high, 'drawdown_pct': (1 - price / high) * 100,
                    'daily': daily, 'weekly': weekly, 'hour': {
                        'open_ms': h[-1]['open_ms'], 'change_pct': (h[-1]['close'] / h[-1]['open'] - 1) * 100,
                        'volume_ratio': h[-1]['volume'] / meanvol if meanvol else None},
                    'background': background}
        try:
            other = float(background['coinbase_spot']['data']['amount'])
            snapshot['cross_exchange_difference_pct'] = (price / other - 1) * 100
            if abs(snapshot['cross_exchange_difference_pct']) > 2:
                raise ValueError('跨交易所价差超过2%，需要核实')
        except (KeyError, TypeError):
            pass
        return snapshot
