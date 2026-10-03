#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

from monitor.core import Config, iso, read_json, write_json, load_portfolio, display_time, localize_text
from monitor.market import Market
from monitor.runtime import Runner, Lock
from monitor.telegram import Telegram, Outbox

LABEL = 'local.binbin.btc-spot-monitor'


def service(cfg, action):
    path = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
    domain = 'gui/' + str(os.getuid())
    if action == 'install':
        args = [sys.executable, str(cfg.root / 'run.py'), 'run']
        if cfg.get('KEEP_AWAKE', 'true').lower() == 'true':
            args = ['/usr/bin/caffeinate', '-i'] + args
        content = {'Label': LABEL, 'ProgramArguments': args, 'WorkingDirectory': str(cfg.root),
                   'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 60,
                   'ExitTimeOut': int(cfg.number('CODEX_TIMEOUT_SECONDS', 600)) + 60,
                   'StandardOutPath': str(cfg.runtime / 'launchd.stdout.log'),
                   'StandardErrorPath': str(cfg.runtime / 'launchd.stderr.log')}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps(content))
        os.chmod(path, 0o600)
        print('后台配置已安装（不包含凭证）：', path)
    elif action == 'start':
        if not path.exists():
            raise RuntimeError('先运行 service install')
        check = subprocess.run(['launchctl', 'print', domain + '/' + LABEL], capture_output=True)
        if check.returncode == 0:
            print('服务已经加载；使用status检查运行情况')
        else:
            subprocess.run(['launchctl', 'bootstrap', domain, str(path)], check=True)
            print('后台服务已启动')
    elif action == 'stop':
        subprocess.run(['launchctl', 'bootout', domain + '/' + LABEL], check=True)
        print('后台服务已停止')
    elif action == 'status':
        p = subprocess.run(['launchctl', 'print', domain + '/' + LABEL], capture_output=True, text=True)
        if p.returncode:
            print('后台服务未加载')
        else:
            # 不打印launchctl完整输出，避免泄露用户继承环境。
            for line in p.stdout.splitlines():
                if line.strip().startswith(('state =', 'pid =', 'runs =', 'last exit code =', 'program =')):
                    print(line.strip())


def main():
    parser = argparse.ArgumentParser(description='BTC现货监控：不自动下单')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ['doctor', 'collect', 'once', 'run', 'status', 'flush', 'retry-delivery']:
        sub.add_parser(name)
    p = sub.add_parser('telegram-test')
    p = sub.add_parser('service')
    p.add_argument('action', choices=['install', 'start', 'stop', 'status'])
    p = sub.add_parser('record-trade', help='仅记录用户已经完成的真实成交，不下单')
    p.add_argument('side', choices=['buy', 'sell'])
    p.add_argument('--quantity', type=float, required=True)
    p.add_argument('--price', type=float, required=True)
    p.add_argument('--fee-usdt', type=float, default=0)
    p.add_argument('--note', default='用户录入真实成交')
    args = parser.parse_args()
    cfg = Config()
    if args.command == 'doctor':
        print('配置完整（凭证不显示）；.env权限0600')
        load_portfolio(cfg.root)
        print(json.dumps(Telegram(cfg).verify(), ensure_ascii=False))
        p = subprocess.run([cfg.get('CODEX_BIN', '/opt/homebrew/bin/codex'), 'login', 'status'],
                           capture_output=True, text=True, env=cfg.child_env())
        print(cfg.redact((p.stdout + p.stderr).strip()))
        if p.returncode:
            raise RuntimeError('Codex尚未登录')
    elif args.command == 'collect':
        with Lock(cfg.runtime / 'monitor.lock'):
            data = Market(cfg).collect()
            write_json(cfg.runtime / 'latest_snapshot.json', data)
            print(json.dumps({'北京时间': display_time(data['collected_at']), 'price': data['price'], 'drawdown_pct': data['drawdown_pct']}, ensure_ascii=False))
            print('完整快照已保存：runtime/latest_snapshot.json')
    elif args.command in ['run', 'once']:
        Runner(cfg).run(once=args.command == 'once')
    elif args.command == 'telegram-test':
        tg = Telegram(cfg)
        print(json.dumps(tg.verify(), ensure_ascii=False))
        tg.send('【接入测试】BTC现货监控机器人连接成功。此消息不是投资建议。时间：' + display_time() + ' 北京时间')
        print('测试消息已发送')
    elif args.command == 'status':
        s = read_json(cfg.runtime / 'state.json', {})
        for k in ['last_collection', 'last_success', 'latest_price', 'running_report', 'last_error', 'stopped_at']:
            v = s.get(k)
            if k in ['last_collection', 'last_success'] and v:
                v = display_time(v) + ' 北京时间'
            print(k, ':', cfg.redact(localize_text(str(v))))
        print('telegram:', Outbox(cfg).counts())
        print('最近核实的现货持仓（非实时）:', localize_text(json.dumps(s.get('latest_verified_portfolio'), ensure_ascii=False)))
        print('本地预算与成交记录:', localize_text(json.dumps(load_portfolio(cfg.root), ensure_ascii=False)))
    elif args.command in ['flush', 'retry-delivery']:
        with Lock(cfg.runtime / 'monitor.lock'):
            out = Outbox(cfg)
            if args.command == 'retry-delivery':
                out.retry_failed()
            for _ in range(30):
                out.flush()
                if not out.counts()['pending']:
                    break
                time.sleep(2)
            print(out.counts())
    elif args.command == 'service':
        service(cfg, args.action)
    elif args.command == 'record-trade':
        import math
        if not all(math.isfinite(x) for x in [args.quantity, args.price, args.fee_usdt]) or min(args.quantity, args.price) <= 0 or args.fee_usdt < 0:
            raise ValueError('数量、价格必须为正，手续费不能为负')
        with Lock(cfg.runtime / 'portfolio.lock'):
            p = load_portfolio(cfg.root)
            qty, cost = p['btc_quantity'], p.get('average_cost_usdt')
            cash = p.get('remaining_budget_usdt')
            notional = args.quantity * args.price
            if args.side == 'buy':
                if cash is not None and notional + args.fee_usdt > cash:
                    raise ValueError('买入金额超过记录的剩余资金，先核实预算')
                new = qty + args.quantity
                p['average_cost_usdt'] = ((qty * cost if qty else 0) + notional + args.fee_usdt) / new if not qty or cost is not None else None
                p['btc_quantity'] = new
                if cash is not None:
                    p['remaining_budget_usdt'] = cash - notional - args.fee_usdt
            else:
                if args.quantity > qty or args.fee_usdt > notional:
                    raise ValueError('卖出数量超过实际持仓或手续费无效')
                p['btc_quantity'] = max(0, qty - args.quantity)
                if p['btc_quantity'] == 0:
                    p['average_cost_usdt'] = None
                if cash is not None:
                    p['remaining_budget_usdt'] = cash + notional - args.fee_usdt
            p.setdefault('trades', []).append({'time': iso(), 'side': args.side, 'quantity': args.quantity,
                                              'price': args.price, 'fee_usdt': args.fee_usdt, 'note': args.note})
            p['updated_at'] = iso()
            write_json(cfg.root / 'portfolio.json', p)
            print('已更新实际成交记录；没有发送任何交易订单')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # 不打印traceback/局部变量，避免HTTP异常携带凭证。
        print('失败：' + type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
        sys.exit(1)
