from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import time
import tempfile
from pathlib import Path

from .core import iso, write_json, read_json, display_time, localize_text


from .funding import rules, check_purchase, budget_amount
from .balance import account_command, query_balance, verified_portfolio


NARRATIVE_FIELDS = ['market_summary', 'reason_summary', 'event_summary', 'next_watch', 'changes', 'limitations']


def schema():
    props = {
        'balance': {'type': 'object', 'properties': {
            'status': {'type': 'string', 'enum': ['verified', 'unavailable']},
            'usdc_free': {'type': ['string', 'null']}, 'usdc_locked': {'type': ['string', 'null']},
            'btc_free': {'type': ['string', 'null']}, 'btc_locked': {'type': ['string', 'null']},
            'queried_at': {'type': ['string', 'null']}},
            'required': ['status', 'usdc_free', 'usdc_locked', 'btc_free', 'btc_locked', 'queried_at'], 'additionalProperties': False},
        'position_mode': {'type': 'string', 'enum': ['flat', 'holding', 'unknown']},
        'action': {'type': 'string', 'enum': ['买入', '加仓', '补仓', '减仓', '持有', '等待']},
        'buy_decision': {'type': 'string', 'enum': ['买', '不买', '不适用']},
        'reduce_pct': {'type': 'number', 'minimum': 0, 'maximum': 100},
        'buy_budget_pct': {'type': 'number', 'minimum': 0, 'maximum': 100},
        'add_remaining_pct': {'type': 'number', 'minimum': 0, 'maximum': 100},
        'execution': {'type': 'string', 'enum': ['现在', '条件触发', '等待']},
        'entry_low': {'type': ['number', 'null']}, 'entry_high': {'type': ['number', 'null']},
        'valid_for_minutes': {'type': 'integer', 'minimum': 1, 'maximum': 120},
        'execution_conditions': {'type': 'string'}, 'invalidation': {'type': 'string'},
        **{name: {'type': 'string'} for name in NARRATIVE_FIELDS},
        'sources': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'title': {'type': 'string'}, 'url': {'type': 'string'}},
            'required': ['title', 'url'], 'additionalProperties': False}},
    }
    return {'type': 'object', 'properties': props, 'required': list(props), 'additionalProperties': False}


def validate(report, portfolio):
    if not isinstance(report, dict) or set(report) != set(schema()['required']):
        raise ValueError('Codex输出字段不完整')
    for name in ['reduce_pct', 'buy_budget_pct', 'add_remaining_pct']:
        v = report[name]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 100:
            raise ValueError('Codex比例无效')
    if type(report['valid_for_minutes']) is not int or not 1 <= report['valid_for_minutes'] <= 120:
        raise ValueError('报告有效期无效')
    for name in ['execution_conditions', 'invalidation'] + NARRATIVE_FIELDS:
        if not isinstance(report[name], str) or not report[name].strip():
            raise ValueError('Codex输出缺少分析依据')
    if report['balance']['status'] != 'verified':
        raise ValueError('余额未知，暂停仓位建议')
    if report['execution'] not in ['现在', '条件触发', '等待']:
        raise ValueError('执行方式无效')
    flat = portfolio['btc_quantity'] == 0
    if report['position_mode'] != ('flat' if flat else 'holding'):
        raise ValueError('分析与实际仓位不一致')
    if flat:
        if report['buy_decision'] not in ['买', '不买'] or report['reduce_pct'] or report['add_remaining_pct']:
            raise ValueError('空仓结论无效')
        if report['buy_decision'] == '买':
            if report['buy_budget_pct'] <= 0 or report['execution'] != '现在':
                raise ValueError('买入结论与执行条件矛盾')
        elif report['buy_budget_pct']:
            raise ValueError('不买时不得分配买入比例')
    elif report['buy_decision'] != '不适用' or report['buy_budget_pct']:
        raise ValueError('持仓结论无效')
    if report['reduce_pct'] and report['add_remaining_pct']:
        raise ValueError('不能同时加仓和减仓')
    if report['execution'] == '等待' and any(report[k] > 0 for k in ['reduce_pct', 'buy_budget_pct', 'add_remaining_pct']):
        raise ValueError('等待状态与操作比例矛盾')
    low, high = report['entry_low'], report['entry_high']
    if (low is None) != (high is None):
        raise ValueError('入场区间不完整')
    if low is not None and (any(isinstance(v, bool) or not isinstance(v, (int, float)) or
                                not math.isfinite(v) for v in [low, high]) or not 0 < low <= high):
        raise ValueError('入场区间无效')
    if (report['buy_budget_pct'] or report['add_remaining_pct']) and low is None:
        raise ValueError('买入必须提供价格区间')
    expected = ('买入' if report['buy_budget_pct'] else '减仓' if report['reduce_pct']
                else '加仓' if report['add_remaining_pct'] else '等待' if flat else '持有')
    if report['action'] != expected and not (expected == '加仓' and report['action'] == '补仓'):
        raise ValueError('操作类型与实际仓位或比例矛盾')
    if report['action'] == '补仓':
        cost, price = portfolio.get('average_cost_usdt'), portfolio.get('reference_price_usdt')
        if cost is None or price is None or not price < cost or high is None or high >= cost:
            raise ValueError('补仓须核实成本且当前价格及买入区间低于成本')
    purchase_pct = report['buy_budget_pct'] or report['add_remaining_pct']
    if purchase_pct:
        check_purchase(portfolio, purchase_pct)
    if not isinstance(report['sources'], list) or any(
        not isinstance(s, dict) or not isinstance(s.get('url'), str) or not s['url'].startswith('https://')
        for s in report['sources']):
        raise ValueError('来源格式无效')


def analyze(cfg, snapshot, portfolio, reasons, previous, events, report_id):
    # 输入及结果仅在本次调用期间存在；成功、失败、超时均清理。
    with tempfile.TemporaryDirectory(prefix='btc-spot-analysis-') as directory:
        return _analyze(cfg, snapshot, portfolio, reasons, previous, events, Path(directory))


def _analyze(cfg, snapshot, portfolio, reasons, previous, events, work):
    requested_at = time.time()
    funding_rules = rules(cfg)
    # 分析目录只放数据和Skill，不复制.env；CLI从该目录工作。
    skill = work / 'SKILL.md'
    skill.write_text((cfg.root / 'skills/btc-spot-position-advisor/SKILL.md').read_text(), encoding='utf-8')
    write_json(work / 'schema.json', schema())
    write_json(work / 'input.json', {'snapshot': snapshot, 'portfolio_metadata_only': {k: v for k, v in portfolio.items()
                                       if k not in ['trades', 'planned_budget_usdt', 'remaining_budget_usdt']},
                                   'budget_policy': '现货USDC可用余额是用户授权的剩余BTC资金；冻结USDC不可用，USDT预算字段不参与计算',
                                   'funding_rules': funding_rules, 'balance_query_argv': account_command(cfg), 'reasons': reasons,
                                   'previous_analysis_not_execution': previous, 'events': events,
                                   'analysis_requested_at': iso(), 'timezone': 'Asia/Shanghai'})
    prompt = ('使用 $btc-spot-position-advisor 技能，完整读取当前目录SKILL.md和input.json，'
              '按照schema.json输出BTC现货长期持仓分析。必须尝试使用web搜索核实最新消息、ETF及宏观背景。'
              '必须先执行input.json的balance_query_argv，使用Binance CLI先核对USDC可用余额，再核对BTC持仓，然后给买入、加仓、补仓、减仓或等待建议。'
              '仅允许CLI使用既有认证完成该只读查询；不得直接查看.env、密钥或CLI账户配置文件。'
              '不执行交易，不发送Telegram，不改写文件。预算未知时不编造金额。只返回符合Schema的JSON。')
    cmd = [cfg.get('CODEX_BIN', '/opt/homebrew/bin/codex'), '--no-daemon', '--search', '-a', 'never',
           'exec', '--sandbox', 'read-only', '--skip-git-repo-check', '--ephemeral',
           '-C', str(work), '--output-schema', str(work / 'schema.json'),
           '-o', str(work / 'answer.json'), '--json']
    if cfg.get('CODEX_MODEL'):
        cmd += ['-m', cfg.get('CODEX_MODEL')]
    if cfg.get('CODEX_REASONING_EFFORT'):
        cmd += ['-c', 'model_reasoning_effort=' + json.dumps(cfg.get('CODEX_REASONING_EFFORT'))]
    cmd += ['-']
    with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          text=True, env=cfg.child_env(), start_new_session=True) as proc:
        try:
            proc.communicate(prompt, timeout=cfg.number('CODEX_TIMEOUT_SECONDS', 600))
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
            raise RuntimeError('Codex分析超时') from None
    if proc.returncode:
        raise RuntimeError('Codex退出码=' + str(proc.returncode) + '，本次未生成有效报告（不保留分析中间文件）')
    result = read_json(work / 'answer.json')
    portfolio = verified_portfolio(portfolio, result.get('balance'), query_balance(cfg), requested_at)
    portfolio.update(funding_rules=funding_rules, reference_price_usdt=snapshot['price'])
    validate(result, portfolio)
    if result['execution'] == '现在' and (result['buy_budget_pct'] or result['add_remaining_pct']) and not result['entry_low'] <= snapshot['price'] <= result['entry_high']:
        raise ValueError('买入结论不包含当前价格，不能把未来条件当作现在买入')
    # 防止模型输出意外含有凭证或外部内容伪造配置值。
    result = json.loads(cfg.redact(json.dumps(result, ensure_ascii=False)))
    validate(result, portfolio)
    if time.time() - snapshot['collected_at'] > cfg.number('MAX_SNAPSHOT_AGE_SECONDS', 900):
        raise RuntimeError('分析完成时行情快照已过期，丢弃操作建议并重试')
    return result, portfolio


def render(record):
    r, p = record['analysis'], record['portfolio']
    headline = ('空仓 · ' + r['buy_decision']) if p['btc_quantity'] == 0 else '持仓 · ' + r.get('action', f"建议减仓 {r['reduce_pct']:g}%")
    sections = ['₿ BTC 现货｜' + headline + '\n长期持有 · 分批操作']
    if p['btc_quantity'] == 0:
        action = ['📌 空仓建议', '当前结论：' + r['buy_decision'], '执行方式：' + r['execution']]
        if r['buy_decision'] == '买':
            action.append(f"首笔投入：可用 USDC 的 {r['buy_budget_pct']:g}%")
        else:
            action.append('本次投入：0% · 继续等待')
    else:
        qty = p['btc_quantity'] * r['reduce_pct'] / 100
        action = ['📌 持仓建议', f"当前持仓：{p['btc_quantity']:.8f} BTC", '执行方式：' + r['execution'],
                  f"建议减仓：当前持仓的 {r['reduce_pct']:g}%" + (' · 暂不减仓' if not qty else ''),
                  f"对应卖出：{qty:.8f} BTC", f"执行后剩余：{p['btc_quantity'] - qty:.8f} BTC",
                  f"加仓／补仓投入：可用 USDC 的 {r['add_remaining_pct']:g}%"]
    if 'usdc_free' in p:
        pct = r['buy_budget_pct'] or r['add_remaining_pct']
        action.extend([f"USDC 总额：{float(p['usdc_free']) + float(p['usdc_locked']):.8f}",
                       f"可用资金：{p['usdc_free']} USDC · 冻结：{p['usdc_locked']} USDC",
                       f"本次操作：{r.get('action', '等待')}",
                       f"本次资金上限：{budget_amount(p, pct):.8f} USDC（含手续费预留）"])
        if p.get('average_cost_usdt') is None and p['btc_quantity']:
            action.append('成本未知：不认定为降低成本的补仓。')
        fr = p.get('funding_rules', {})
        if fr.get('status') == 'verified':
            action.append(f"BTC/USDC 最低名义金额：{fr['min_notional']} USDC（另需满足数量规则）")
        else:
            action.append('BTC/USDC 交易规则未知，暂停新增买入建议。')
    if r['entry_low'] is not None:
        label = '买入区间' if r['buy_budget_pct'] or r['add_remaining_pct'] else '观察区间（非买入指令）'
        action.append(f"{label}：{r['entry_low']:,.2f}～{r['entry_high']:,.2f} USDT")
    if p.get('balance_source'):
        action.extend(['仓位来源：' + p['balance_source'],
                       '余额核对：' + display_time(p['balance_checked_at']) + ' 北京时间',
                       f"可用：{p['btc_free']:.8f} BTC · 冻结：{p['btc_locked']:.8f} BTC",
                       '范围：仅现货钱包，不含理财、资金钱包及其他子账户'])
        if p['btc_quantity'] * r['reduce_pct'] / 100 > p['btc_free']:
            action.append('注意：建议减仓量超过可用余额，冻结部分不可立即卖出。')
    sections.append('\n'.join(action))
    clock = ['🕒 报告时间｜全部为北京时间',
             '行情截至：' + display_time(record['snapshot_time']),
             '分析完成：' + display_time(record['completed_at']),
             '有效截止：' + display_time(record['valid_until'])]
    sections.append('\n'.join(clock))
    market = ['📊 具体行情']
    snap = record.get('snapshot', {})
    if snap:
        market.append(f"BTC/USDT：{snap['price']:,.2f}")
        rolling = snap.get('rolling', {})
        for windows in [[('15m', '15分钟'), ('1h', '1小时')], [('1d', '24小时'), ('7d', '7天')]]:
            market.append('  ｜  '.join(f"{label}：{rolling[key]['priceChangePercent']:+.2f}%"
                                       for key, label in windows if key in rolling))
        if 'drawdown_pct' in snap:
            market.append(f"距90日高点回撤：{snap['drawdown_pct']:.2f}%")
        daily = snap.get('daily', {})
        if all(k in daily for k in ['rsi14', 'atr14', 'ma20', 'ma200']):
            market.extend([f"日线 RSI14：{daily['rsi14']:.1f}  ｜  ATR14：{daily['atr14']:,.2f} USDT",
                           f"日线 MA20：{daily['ma20']:,.2f}  ｜  MA200：{daily['ma200']:,.2f}"])
        if daily.get('close_ms'):
            market.append('日线收盘：' + display_time(daily['close_ms'] / 1000))
    else:
        market.append('旧报告未保存独立行情快照，详见下方原始分析。')
    if r.get('market_summary'):
        market.append(r['market_summary'])
    sections.append('\n'.join(market))
    sections.append('💡 判断原因\n' + r.get('reason_summary', r.get('report_markdown', '暂无说明')))
    if r.get('event_summary'):
        sections.append('🌐 事件与资金\n' + r['event_summary'])
    conditions = ['🎯 执行与后续关注', '执行条件：' + r['execution_conditions']]
    if r.get('next_watch'):
        conditions.append('下一步：' + r['next_watch'])
    conditions.append('失效条件：' + r['invalidation'])
    sections.append('\n'.join(conditions))
    if r.get('changes'):
        sections.append('🔄 与上次相比\n' + r['changes'])
    if r.get('limitations'):
        sections.append('⚠️ 信息局限\n' + r['limitations'])
    if r.get('sources'):
        sections.append('🔗 核实来源\n' + '\n'.join(f"{i}. {x.get('title', '来源')}\n{x['url']}"
                                                   for i, x in enumerate(r['sources'], 1)))
    sections.append('触发原因：' + ('；'.join(record['reasons']) or '定时复核') + '\n报告编号：' + record['id'])
    return localize_text('\n\n'.join(sections))
