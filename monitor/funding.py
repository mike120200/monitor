"""USDC budget arithmetic and indicative BTCUSDC minimum-order checks."""
from decimal import Decimal, ROUND_DOWN
from .core import Http, iso
from .balance import amount


def rules(cfg):
    try:
        http = Http(cfg)
        base = cfg.get('BINANCE_BASE_URL', 'https://api.binance.com').rstrip('/')
        data = http.request(base + '/api/v3/exchangeInfo?symbol=BTCUSDC')
        ticker = http.request(base + '/api/v3/ticker/price?symbol=BTCUSDC')
        symbol = data['symbols'][0]
        if symbol['symbol'] != 'BTCUSDC' or symbol['status'] != 'TRADING' or ticker['symbol'] != 'BTCUSDC':
            raise ValueError('交易对不可用')
        fs = {x['filterType']: x for x in symbol['filters']}
        minimums = [amount(fs[k]['minNotional']) for k in ['NOTIONAL', 'MIN_NOTIONAL'] if k in fs]
        lot = fs['LOT_SIZE']
        price, step = amount(ticker['price']), amount(lot['stepSize'])
        reserve = Decimal(str(cfg.number('USDC_FEE_RESERVE_PCT', 0.2)))
        if not minimums or price <= 0 or step <= 0 or not 0 <= reserve < 100:
            raise ValueError('规则无效')
        return {'status': 'verified', 'symbol': 'BTCUSDC', 'price': str(price),
                'min_notional': str(max(minimums)), 'min_qty': str(amount(lot['minQty'])),
                'max_qty': str(amount(lot['maxQty'])), 'step_size': str(step),
                'max_notional': fs.get('NOTIONAL', {}).get('maxNotional'),
                'reserve_pct': str(reserve), 'checked_at': iso()}
    except Exception:
        return {'status': 'unavailable', 'symbol': 'BTCUSDC', 'reason': '未能核实交易规则或报价，暂停新增买入建议'}


def budget_amount(portfolio, pct):
    return (amount(portfolio['usdc_free']) * Decimal(str(pct)) / 100).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)


def check_purchase(portfolio, pct):
    r = portfolio.get('funding_rules', {})
    if r.get('status') != 'verified':
        raise ValueError('BTCUSDC规则未核实，不能给出新增买入建议')
    budget = budget_amount(portfolio, pct)
    notional = budget * (1 - Decimal(r['reserve_pct']) / 100)
    price, step = amount(r['price']), amount(r['step_size'])
    qty = (notional / price / step).to_integral_value(rounding=ROUND_DOWN) * step
    actual = qty * price
    if budget <= 0 or qty <= 0 or qty < amount(r['min_qty']) or actual < amount(r['min_notional']):
        raise ValueError('USDC建议金额不足最低交易要求，不能给出买入/加仓/补仓建议')
    if qty > amount(r['max_qty']) or (r.get('max_notional') and actual > amount(r['max_notional'])):
        raise ValueError('建议单笔金额超过BTCUSDC交易限制')
    return budget
