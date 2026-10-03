from __future__ import annotations

from .core import stamp


def evaluate(snapshot, state, cfg, events, now):
    """Mutates persisted trigger state; returns currently pending reason dictionaries."""
    r = {k: v['priceChangePercent'] for k, v in snapshot['rolling'].items()}
    d, h = snapshot['daily'], snapshot['hour']
    n = cfg.number
    conditions = {
        'drop_15m': (r['15m'] <= -n('DROP_15M_PCT', 2), False, '15分钟快速下跌'),
        'drop_1h': (r['1h'] <= -n('DROP_1H_PCT', 3), False, '1小时快速下跌'),
        'drop_24h': (r['1d'] <= -n('DROP_24H_PCT', 6), False, '24小时快速下跌'),
        'severe_1h': (r['1h'] <= -n('SEVERE_DROP_1H_PCT', 5), True, '1小时严重下跌'),
        'severe_24h': (r['1d'] <= -n('SEVERE_DROP_24H_PCT', 10), True, '24小时严重下跌'),
        'rise_24h': (r['1d'] >= n('RISE_24H_PCT', 6), False, '24小时快速上涨'),
        'rise_7d': (r['7d'] >= n('RISE_7D_PCT', 15), False, '7天快速上涨'),
        'sell_volume': (h['change_pct'] <= -n('VOLUME_DROP_PCT', 2) and
                        h['volume_ratio'] is not None and h['volume_ratio'] >= n('VOLUME_RATIO', 2),
                        False, '已收盘小时K放量抛售'),
        'hot': (d['rsi14'] >= n('HOT_RSI', 75) and d['distance_ma20_pct'] >= n('HOT_MA20_DISTANCE_PCT', 15),
                False, '日线RSI和均线偏离同时过热'),
    }
    latches = state.setdefault('latches', {})
    pending = state.setdefault('pending', {})
    live = set()
    for key, (on, urgent, label) in conditions.items():
        latch = latches.setdefault(key, {'active': False, 'off_count': 0})
        if on:
            live.add(key)
            latch['off_count'] = 0
            if not latch['active']:
                latch['active'] = True
                pending[key] = {'key': key, 'label': label, 'urgent': urgent, 'at': now}
        else:
            latch['off_count'] += 1
            if latch['off_count'] >= 3:
                latch['active'] = False

    levels = sorted(float(x) for x in cfg.get('DRAWDOWN_LEVELS', '15,20,30,40').split(','))
    previous_price = state.get('previous_price')
    for level in levels:
        key = f'drawdown_{level:g}'
        latch = latches.setdefault(key, {'active': False, 'off_count': 0})
        dd = snapshot['drawdown_pct']
        if dd >= level:
            live.add(key)
            # 同一新高点口径下比较前后价格，过滤仅由参考窗口变化造成的跨档。
            prev_dd = (1 - previous_price / snapshot['high90']) * 100 if previous_price else -1
            crossed = previous_price is None or prev_dd < level <= dd
            if crossed and not latch['active']:
                latch.update(active=True, off_count=0, anchor=snapshot['high90'])
                pending[key] = {'key': key, 'label': f'90天高点回撤进入{level:g}%档',
                                'urgent': level > levels[0], 'at': now}
        # 以触发时冻结高点计算恢复，避免旧高点移出窗口导致虚假恢复。
        anchor = latch.get('anchor', snapshot['high90'])
        recovery_dd = (1 - snapshot['price'] / anchor) * 100
        if latch['active'] and dd < level - 2 and recovery_dd < level - 2:
            latch['off_count'] += 1
            if latch['off_count'] >= 3:
                latch.update(active=False, off_count=0)
        else:
            latch['off_count'] = 0
    state['previous_price'] = snapshot['price']

    # 日线交叉仅在新日K首次出现时判断，不把同一根交叉连续触发多次。
    day_key = str(d['close_ms'])
    if snapshot['drawdown_pct'] >= levels[0]:
        recovery = (d['rsi14_prev'] < n('RECOVERY_RSI', 30) <= d['rsi14'] or
                    d['previous_close'] <= d['ma20_prev'] and d['close'] > d['ma20'])
        key = 'recovery_' + day_key
        if recovery:
            live.add(key)
            if state.get('seen_recovery') != day_key:
                pending[key] = {'key': key, 'label': '深度回撤后的日线修复', 'urgent': False, 'at': now}
                state['seen_recovery'] = day_key

    seen_events = state.setdefault('seen_events', [])
    for e in events:
        if not e.get('verified') or not e.get('sources'):
            continue
        event_at = stamp(e['time_utc'])
        for stage, offset, end in [('before', -1800, 0), ('after', 900, 3600)]:
            key = 'event_' + e['id'] + '_' + stage
            if event_at + offset <= now < event_at + end:
                live.add(key)
                if key not in seen_events:
                    pending[key] = {'key': key, 'label': e['name'] + ('公布前' if stage == 'before' else '公布后'),
                                    'urgent': True, 'at': now}
                    seen_events.append(key)
    state['seen_events'] = seen_events[-500:]
    state['pending'] = {k: v for k, v in pending.items() if k in live}
    return list(state['pending'].values())


def should_analyze(state, reasons, cfg, now):
    if now < state.get('retry_after', 0):
        return False
    last = state.get('last_success', 0)
    return (not last or now - last >= cfg.number('ANALYSIS_INTERVAL_SECONDS', 21600) or
            any(r['urgent'] for r in reasons) or
            bool(reasons) and now - last >= cfg.number('COOLDOWN_SECONDS', 3600))
