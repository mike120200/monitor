"""Read-only verification of the balance Codex uses; never submit orders."""
import json
import subprocess
from decimal import Decimal, InvalidOperation

from .core import iso, stamp


def account_command(cfg):
    cmd = [cfg.get('BINANCE_CLI_BIN', '/Users/binbin/.cargo/bin/binance-cli'),
           'spot', 'get-account', '--omit-zero-balances', 'true']
    if cfg.get('BINANCE_CLI_PROFILE'):
        cmd += ['--profile', cfg.get('BINANCE_CLI_PROFILE')]
    return cmd


def amount(value):
    if not isinstance(value, str):
        raise ValueError('余额必须是十进制字符串')
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError('余额格式无效') from None
    if not number.is_finite() or number < 0:
        raise ValueError('余额无效')
    return number


def parse_account(data):
    if not isinstance(data, dict) or data.get('accountType') != 'SPOT' or not isinstance(data.get('balances'), list):
        raise ValueError('未获取到有效现货账户，持仓未知')
    seen, assets = set(), {k: {'free': '0', 'locked': '0'} for k in ['BTC', 'USDC']}
    for row in data['balances']:
        if not isinstance(row, dict) or not isinstance(row.get('asset'), str) or row['asset'] in seen:
            raise ValueError('账户余额响应异常，持仓未知')
        seen.add(row['asset'])
        amount(row.get('free')); amount(row.get('locked'))
        if row['asset'] in assets:
            assets[row['asset']] = row
    # Only a valid successful response with omit-zero-balances permits missing BTC => zero.
    return {'status': 'verified', 'queried_at': iso(),
            **{asset.lower() + '_' + key: assets[asset][key]
               for asset in ['USDC', 'BTC'] for key in ['free', 'locked']}}


def query_balance(cfg):
    try:
        proc = subprocess.run(account_command(cfg), capture_output=True, text=True,
                              timeout=30, env=cfg.child_env())
        if proc.returncode:
            raise RuntimeError('Binance CLI余额查询失败，持仓未知，暂停仓位建议')
        return parse_account(json.loads(proc.stdout))
    except (subprocess.TimeoutExpired, OSError, ValueError):
        raise RuntimeError('Binance CLI余额查询或解析失败，持仓未知，暂停仓位建议') from None


def verified_portfolio(local, claimed, actual, requested_at):
    if not isinstance(claimed, dict) or claimed.get('status') != 'verified':
        raise ValueError('Codex未成功查询余额，持仓未知，暂停仓位建议')
    try:
        queried = stamp(claimed['queried_at'])
        verified = stamp(actual['queried_at'])
        if not requested_at - 5 <= queried <= verified + 5:
            raise ValueError('Codex余额查询时间不属于本次分析')
        for key in ['usdc_free', 'usdc_locked', 'btc_free', 'btc_locked']:
            if amount(claimed[key]) != amount(actual[key]):
                raise ValueError('Codex查询余额与发送前复核不一致，取消旧建议并重试')
    except (KeyError, TypeError):
        raise ValueError('Codex余额字段不完整') from None
    p = dict(local)
    p.update(btc_quantity=float(amount(actual['btc_free']) + amount(actual['btc_locked'])),
             btc_free=float(amount(actual['btc_free'])), btc_locked=float(amount(actual['btc_locked'])),
             average_cost_usdt=None, balance_source='Binance CLI · 当前配置账户现货钱包',
             balance_checked_at=actual['queried_at'])
    p.update(usdc_free=actual['usdc_free'], usdc_locked=actual['usdc_locked'],
             budget_currency='USDC', available_budget_usdc=actual['usdc_free'])
    # 成本仅使用用户明确确认且数量匹配当前持仓的记录。
    if (local.get('average_cost_verified') is True and p['btc_quantity'] > 0
            and amount(str(local.get('btc_quantity', 0))) == amount(actual['btc_free']) + amount(actual['btc_locked'])
            and local.get('average_cost_usdt') is not None and local['average_cost_usdt'] > 0):
        p['average_cost_usdt'] = local['average_cost_usdt']
    return p
