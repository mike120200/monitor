from __future__ import annotations

import time

from .core import Http, write_json, read_json, stamp, iso


class Telegram:
    def __init__(self, cfg):
        self.cfg, self.http = cfg, Http(cfg)

    def call(self, method, body=None):
        url = 'https://api.telegram.org/bot' + self.cfg.get('TELEGRAM_BOT_TOKEN') + '/' + method
        d = self.http.request(url, body)
        if d.get('ok') is not True:
            raise RuntimeError('Telegram返回失败 code=' + str(d.get('error_code', 'unknown')))
        return d['result']

    def verify(self):
        bot = self.call('getMe')
        chat = self.call('getChat', {'chat_id': self.cfg.get('TELEGRAM_CHAT_ID')})
        if str(chat['id']) != self.cfg.get('TELEGRAM_CHAT_ID'):
            raise ValueError('Telegram目标不匹配')
        return {'bot_username': bot.get('username'), 'chat_type': chat.get('type'), 'target_verified': True}

    def send(self, text):
        return self.call('sendMessage', {'chat_id': self.cfg.get('TELEGRAM_CHAT_ID'),
                        'text': text, 'link_preview_options': {'is_disabled': True}})


def chunks(text, limit=3000):
    # 优先按完整板块/行拆分；超长单行才硬切，保留原文且不拆代理对。
    parts = []
    while text:
        size, end = 0, 0
        for char in text:
            units = len(char.encode('utf-16-le')) // 2
            if size + units > limit:
                break
            size += units
            end += 1
        if not end:
            raise ValueError('分段上限不足以容纳单个字符')
        if end < len(text):
            cut = text.rfind('\n\n', 0, end)
            if cut >= 0:
                end = cut + 2
            else:
                cut = text.rfind('\n', 0, end)
                if cut >= 0:
                    end = cut + 1
        parts.append(text[:end])
        text = text[end:]
    return parts


class Outbox:
    def __init__(self, cfg):
        self.cfg, self.tg = cfg, Telegram(cfg)
        self.directory = cfg.runtime / 'outbox'
        self.directory.mkdir(exist_ok=True)

    def enqueue(self, key, text, valid_until):
        path = self.directory / (key + '.json')
        if path.exists():
            return
        write_json(path, {'id': key, 'parts': chunks(text), 'valid_until': valid_until,
                          'next_part': 0, 'attempts': 0, 'retry_at': 0,
                          'status': 'pending', 'created_at': iso()})

    def flush(self):
        for path in sorted(self.directory.glob('*.json')):
            item = read_json(path)
            if item['status'] != 'pending' or item['retry_at'] > time.time():
                continue
            try:
                # 分段持久化发送游标。网络超时无法确认服务端是否接收，重复风险用报告ID标识。
                i = item['next_part']
                expired = time.time() > stamp(item['valid_until'])
                prefix = '【历史报告，已过有效期，不作为当前操作建议】\n' if expired else ''
                text = prefix + f"📨 BTC 现货报告 · {i+1}/{len(item['parts'])}\n编号：{item['id']}\n\n" + item['parts'][i]
                self.tg.send(text)
                item['next_part'] += 1
                item['attempts'] = 0
                item['retry_at'] = time.time() + 2
                if item['next_part'] == len(item['parts']):
                    item['status'] = 'sent'
                    item['sent_at'] = iso()
            except Exception as exc:
                item['attempts'] += 1
                item['last_error'] = self.cfg.redact(str(exc))
                item['retry_at'] = time.time() + min(30 * 2 ** (item['attempts'] - 1), 1800)
                if item['attempts'] >= int(self.cfg.number('TELEGRAM_MAX_ATTEMPTS', 8)):
                    item['status'] = 'failed'
            write_json(path, item)

    def counts(self):
        counts = {'pending': 0, 'sent': 0, 'failed': 0}
        for path in self.directory.glob('*.json'):
            counts[read_json(path)['status']] += 1
        return counts

    def retry_failed(self):
        for path in self.directory.glob('*.json'):
            item = read_json(path)
            if item['status'] == 'failed':
                item.update(status='pending', attempts=0, retry_at=0)
                write_json(path, item)
