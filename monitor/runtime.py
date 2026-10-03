from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from .advisor import analyze, render
from .core import iso, read_json, write_json, load_portfolio, stamp, display_time
from .market import Market
from .telegram import Outbox
from .triggers import evaluate, should_analyze


def beijing(ts):
    return datetime.fromtimestamp(ts, ZoneInfo('Asia/Shanghai')).isoformat()


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


class Lock:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.file = open(self.path, 'a+')
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise RuntimeError('已有实例运行；请先停止后台服务') from None
        self.file.seek(0)
        self.file.truncate()
        self.file.write(str(os.getpid()))
        self.file.flush()
        return self

    def __exit__(self, *args):
        fcntl.flock(self.file, fcntl.LOCK_UN)
        self.file.close()


class Runner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state_path = cfg.runtime / 'state.json'
        self.state = read_json(self.state_path, {})
        self.state.pop('running_report', None)
        self.market, self.outbox = Market(cfg), Outbox(cfg)
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.future, self.job = None, None
        self.last_job_ok = None
        self.stop_requested = False
        # 完成报告先原子落盘，再更新state；恢复崩溃窗口内未提交的成功时间和消息。
        for path in sorted((cfg.runtime / 'reports').glob('*.json')):
            record = read_json(path)
            if record['completed_ts'] > self.state.get('last_success', 0):
                self.state.update(last_success=record['completed_ts'], previous=record['analysis'])
                self.state['pending'] = {k: v for k, v in self.state.get('pending', {}).items()
                                         if v['at'] > record['started_ts']}
            if record['completed_ts'] >= self.state.get('last_success', 0) and record['portfolio'].get('balance_source'):
                self.state['latest_verified_portfolio'] = record['portfolio']
            self.outbox.enqueue(record['id'], render(record), record['valid_until'])

    def log(self, text):
        message = f'{display_time()} 北京时间 {self.cfg.redact(text)}'
        print(message, flush=True)
        path = self.cfg.runtime / 'monitor.log'
        if path.exists() and path.stat().st_size > 5_000_000:
            path.replace(self.cfg.runtime / 'monitor.previous.log')
        with path.open('a') as f:
            f.write(message + '\n')

    def save(self):
        write_json(self.state_path, self.state)

    def error(self, kind, exc):
        now = time.time()
        self.state['last_error'] = {'kind': kind, 'at': iso(), 'message': self.cfg.redact(str(exc))}
        self.log(kind + ': ' + str(exc))
        last = self.state.setdefault('error_notified', {}).get(kind, 0)
        if now - last > 3600:
            self.outbox.enqueue('error-' + str(int(now)) + '-' + kind,
                                '⚠️ 运行异常（不是投资建议）\n时间：' + display_time(now) + ' 北京时间\n' + kind + '\n' + self.cfg.redact(str(exc)), iso(now + 3600))
            self.state['error_notified'][kind] = now

    def events(self):
        events = read_json(self.cfg.root / 'events.json', [])
        # 只接收人工核对日程，不直接让模型写入调度时间。
        for e in events:
            stamp(e['time_utc'])
        return events

    def collect(self):
        snapshot = self.market.collect()
        now = time.time()
        evaluate(snapshot, self.state, self.cfg, self.events(), now)
        self.state['last_collection'] = now
        self.state['latest_price'] = snapshot['price']
        write_json(self.cfg.runtime / 'latest_snapshot.json', snapshot)
        self.save()
        self.log(f"采集成功 BTC={snapshot['price']:.2f} 待处理触发={len(self.state.get('pending', {}))}")
        return snapshot

    def start(self, snapshot, force=False):
        if self.future:
            return
        now = time.time()
        if now - snapshot['collected_at'] > self.cfg.number('MAX_SOURCE_AGE_SECONDS', 180):
            return
        reasons = list(self.state.get('pending', {}).values())
        if not force and not should_analyze(self.state, reasons, self.cfg, now):
            return
        p = load_portfolio(self.cfg.root)
        labels = [r['label'] for r in reasons]
        if not self.state.get('last_success'):
            labels.append('首次完整分析')
        elif now - self.state['last_success'] >= self.cfg.number('ANALYSIS_INTERVAL_SECONDS', 21600):
            labels.append('六小时定时复核')
        if force:
            labels.append('手动请求完整分析')
        key = datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
        self.job = {'id': key, 'snapshot': snapshot, 'portfolio': p, 'reasons': labels,
                    'trigger_versions': {r['key']: r['at'] for r in reasons}, 'started_ts': now}
        self.future = self.pool.submit(analyze, self.cfg, snapshot, p, labels,
                                       self.state.get('previous'), self.events(), key)
        self.state['running_report'] = key
        self.save()
        self.log('Codex开始分析 ' + key)

    def finish(self):
        if not self.future or not self.future.done():
            return
        job = self.job
        try:
            result, actual_portfolio = self.future.result()
            if digest(load_portfolio(self.cfg.root)) != digest(job['portfolio']):
                raise RuntimeError('分析期间实际持仓已更新，旧仓位建议不发送，需要重新分析')
            latest = read_json(self.cfg.runtime / 'latest_snapshot.json', job['snapshot'])
            if latest['collected_at'] > job['snapshot']['collected_at']:
                change = abs(latest['price'] / job['snapshot']['price'] - 1) * 100
                if change > 2:
                    raise RuntimeError('分析期间价格变化超过2%，旧建议取消，需使用新行情重算')
                if result['execution'] == '现在' and (result['buy_budget_pct'] or result['add_remaining_pct']) and not result['entry_low'] <= latest['price'] <= result['entry_high']:
                    raise RuntimeError('当前价格已离开模型给出的买入区间，旧建议取消并重算')
            now = time.time()
            validity = job['snapshot']['collected_at'] + result['valid_for_minutes'] * 60
            if validity <= now:
                raise RuntimeError('模型给出的有效期在分析完成前已到期，需要更新行情重算')
            record = {'id': job['id'], 'analysis': result, 'portfolio': actual_portfolio, 'snapshot': job['snapshot'],
                      'reasons': job['reasons'], 'started_ts': job['started_ts'], 'completed_ts': now,
                      'completed_at': beijing(now), 'snapshot_time': beijing(job['snapshot']['collected_at']),
                      'valid_until': beijing(validity)}
            write_json(self.cfg.runtime / 'reports' / (job['id'] + '.json'), record)
            text = render(record)
            (self.cfg.runtime / 'reports' / (job['id'] + '.md')).write_text(text, encoding='utf-8')
            self.outbox.enqueue(job['id'], text, record['valid_until'])
            self.state.update(last_success=now, previous=result, retry_after=0, analysis_failures=0,
                              latest_verified_portfolio=actual_portfolio)
            if self.state.get('last_error', {}).get('kind') == 'analysis':
                self.state['last_error']['resolved_at'] = iso(now)
            self.last_job_ok = True
            for k, version in job['trigger_versions'].items():
                if self.state.get('pending', {}).get(k, {}).get('at') == version:
                    del self.state['pending'][k]
            self.log('分析成功并进入Telegram发送队列 ' + job['id'])
        except Exception as exc:
            self.last_job_ok = False
            failures = self.state.get('analysis_failures', 0) + 1
            self.state.update(analysis_failures=failures,
                              retry_after=time.time() + min(300 * 2 ** min(failures - 1, 4), 1800))
            self.error('analysis', exc)
        finally:
            self.future, self.job = None, None
            self.state.pop('running_report', None)
            self.save()

    def run(self, once=False):
        def stop(*_):
            self.stop_requested = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        with Lock(self.cfg.runtime / 'monitor.lock'):
            self.outbox.tg.verify()
            self.state['started_at'] = iso()
            self.state.pop('stopped_at', None)
            if self.state.get('last_error') and self.state.get('last_success', 0) > stamp(self.state['last_error']['at']):
                self.state['last_error'].setdefault('resolved_at', iso(self.state['last_success']))
            self.log('启动' + ('单次分析' if once else '后台监控'))
            next_collect, snapshot = 0, None
            while not self.stop_requested or self.future:
                self.finish()
                if time.time() >= next_collect and not self.stop_requested:
                    tick = time.monotonic()
                    try:
                        snapshot = self.collect()
                        self.start(snapshot, force=once)
                    except Exception as exc:
                        snapshot = None
                        self.error('collection', exc)
                        self.save()
                        if once:
                            raise
                    next_collect = time.time() + max(1, self.cfg.number('POLL_SECONDS', 300) - (time.monotonic() - tick))
                self.outbox.flush()
                if once and self.future is None:
                    # 等待正常分段发送完成；失败保留队列供后台/flush重试。
                    for _ in range(30):
                        if not self.outbox.counts()['pending']:
                            break
                        self.outbox.flush()
                        time.sleep(2)
                    break
                if snapshot and not self.future and not once and not self.stop_requested:
                    self.start(snapshot)
                time.sleep(2)
            self.pool.shutdown(wait=True)
            self.state['stopped_at'] = iso()
            self.save()
            self.log('停止完成')
            if once and self.last_job_ok is not True:
                raise RuntimeError('本次分析未成功；成功时钟未推进，错误见runtime/monitor.log')
