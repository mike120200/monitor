from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def iso(ts=None):
    return datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc).isoformat()


def stamp(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('时间必须包含时区')
    return dt.timestamp()


def display_time(value=None):
    """Display only; stored timestamps retain timezone information for scheduling."""
    try:
        ts = time.time() if value is None else (stamp(value) if isinstance(value, str) else value)
        return datetime.fromtimestamp(ts, ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError, OverflowError, OSError):
        return '时间未知'


def localize_text(text):
    # Normalize explicit ISO timestamps even in legacy model prose; never guess a bare local time.
    pattern = r'(?<![0-9])\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})'
    return re.sub(pattern, lambda m: display_time(m.group()) + ' 北京时间', text)


def read_json(path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Config:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.values = {}
        for line in (self.root / '.env').read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[7:]
            key, sep, value = line.partition('=')
            key, value = key.strip(), value.strip()
            if not sep or not re.fullmatch(r'[A-Z][A-Z0-9_]*', key):
                raise ValueError('.env格式错误，仅支持 KEY=VALUE')
            if len(value) >= 2 and value[0] == value[-1] and value[0] in '\"\'':
                value = value[1:-1]
            if '\n' in value or '\r' in value:
                raise ValueError('.env不支持多行值')
            self.values[key] = value
        os.chmod(self.root / '.env', 0o600)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir(exist_ok=True, mode=0o700)
        os.chmod(self.runtime, 0o700)
        for key in ['TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID']:
            if not self.values.get(key):
                raise ValueError(f'.env缺少{key}')
        if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', self.get('TELEGRAM_BOT_TOKEN')):
            raise ValueError('TELEGRAM_BOT_TOKEN格式不正确')
        if not re.fullmatch(r'-?\d+', self.get('TELEGRAM_CHAT_ID')):
            raise ValueError('TELEGRAM_CHAT_ID必须是数字聊天ID')
        for key, default in [('POLL_SECONDS', 300), ('ANALYSIS_INTERVAL_SECONDS', 21600),
                             ('CODEX_TIMEOUT_SECONDS', 600), ('HTTP_TIMEOUT_SECONDS', 25)]:
            if self.number(key, default) <= 0:
                raise ValueError(f'{key}必须大于0')

    def get(self, key, default=''):
        return self.values.get(key, default)

    def number(self, key, default):
        n = float(self.get(key, str(default)))
        if not math.isfinite(n):
            raise ValueError(f'{key}必须为有限数值')
        return n

    def redact(self, text):
        text = str(text)
        for key, val in self.values.items():
            if val and any(word in key for word in ('TOKEN', 'SECRET', 'PASSWORD', 'CHAT_ID', 'PROXY')):
                text = text.replace(val, '[REDACTED]')
        return re.sub(r'bot\d+:[A-Za-z0-9_-]+', 'bot[REDACTED]', text)

    def child_env(self):
        # 白名单：不把机器人凭证、API密钥或当前代理会话身份传给分析进程。
        allowed = ('HOME', 'USER', 'LOGNAME', 'PATH', 'LANG', 'LC_ALL', 'TMPDIR',
                   'SYSTEMROOT', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'CODEX_HOME')
        env = {k: os.environ[k] for k in allowed if k in os.environ}
        env['PATH'] = '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'
        proxy = self.get('HTTP_PROXY_URL')
        if proxy:
            env.update(HTTPS_PROXY=proxy, HTTP_PROXY=proxy, ALL_PROXY=proxy)
        return env


class Http:
    """Use macOS curl certificate store. Credentials only enter curl via stdin."""
    def __init__(self, cfg):
        self.cfg = cfg

    def request(self, url, data=None, raw=False):
        timeout = self.cfg.number('HTTP_TIMEOUT_SECONDS', 25)
        lines = ['silent', 'show-error', 'max-time = ' + str(timeout),
                 'url = ' + json.dumps(url), 'write-out = "\\n%{http_code}"']
        if self.cfg.get('HTTP_PROXY_URL'):
            lines.append('proxy = ' + json.dumps(self.cfg.get('HTTP_PROXY_URL')))
        if data is not None:
            lines += ['request = "POST"', 'header = "Content-Type: application/json"',
                      'data = ' + json.dumps(json.dumps(data, ensure_ascii=False), ensure_ascii=False)]
        retries = 0 if data is not None else int(self.cfg.number('HTTP_RETRIES', 2))
        for attempt in range(retries + 1):
            try:
                p = subprocess.run(['/usr/bin/curl', '--disable', '--config', '-'],
                                   input='\n'.join(lines), capture_output=True, text=True,
                                   timeout=timeout + 5, env=self.cfg.child_env())
                if p.returncode:
                    raise RuntimeError('网络请求失败 curl=' + str(p.returncode))
                body, _, status = p.stdout.rpartition('\n')
                if status != '200':
                    # 不记录URL或Telegram服务回显中的凭证。
                    raise RuntimeError('HTTP状态=' + status)
                return body if raw else json.loads(body)
            except (RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
                if attempt == retries:
                    message = str(exc) if isinstance(exc, RuntimeError) else '网络超时或返回JSON无效'
                    raise RuntimeError(self.cfg.redact(message)) from None
                time.sleep(min(2 ** attempt, 4))


def load_portfolio(root):
    p = read_json(root / 'portfolio.json')
    if not isinstance(p, dict):
        raise ValueError('缺少portfolio.json')
    for key in ['btc_quantity', 'average_cost_usdt', 'remaining_budget_usdt', 'planned_budget_usdt']:
        v = p.get(key)
        if key == 'btc_quantity' and v is None:
            raise ValueError('实际BTC数量缺失')
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or
                              not math.isfinite(v) or v < 0):
            raise ValueError('持仓字段不正确：' + key)
    if p['btc_quantity'] > 0 and p.get('average_cost_usdt') is None:
        p = dict(p, limitation='持仓成本未知，不提供基于成本的盈利断言')
    return p
