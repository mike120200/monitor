import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from monitor.core import Config, Http, write_json, read_json
from monitor.market import rsi, wilder, indicators
from monitor.triggers import evaluate, should_analyze
from monitor.advisor import validate, render
from monitor.telegram import chunks, Outbox


class Settings:
    def __init__(self, values=None): self.values = values or {}
    def get(self, key, default=''): return self.values.get(key, default)
    def number(self, key, default): return float(self.get(key, default))
    def redact(self, value): return str(value)


def snapshot(price=90000, high=100000):
    return {'price': price, 'high90': high, 'drawdown_pct': (1-price/high)*100,
            'rolling': {w: {'priceChangePercent': 0} for w in ['15m','1h','1d','7d']},
            'daily': {'rsi14': 50, 'rsi14_prev': 50, 'distance_ma20_pct': 0, 'close_ms': 1,
                      'previous_close': 90, 'ma20_prev': 100, 'close': 90, 'ma20': 100},
            'hour': {'change_pct': 0, 'volume_ratio': 1}}


def report():
    return {'balance': {'status':'verified','usdc_free':'0','usdc_locked':'0','btc_free':'0','btc_locked':'0','queried_at':'2026-10-03T00:00:00Z'}, 'action':'等待', 'position_mode': 'flat', 'buy_decision': '不买', 'reduce_pct': 0,
            'buy_budget_pct': 0, 'add_remaining_pct': 0, 'execution': '等待',
            'entry_low': None, 'entry_high': None, 'valid_for_minutes': 120,
            'execution_conditions': '等确认', 'invalidation': '支撑失效', **{k: '分析' for k in ['market_summary', 'reason_summary', 'event_summary', 'next_watch', 'changes', 'limitations']}, 'sources': []}


def funded_portfolio():
    return {'btc_quantity':0, 'usdc_free':'1000', 'usdc_locked':'0',
            'funding_rules': {'status':'verified','price':'80000','min_notional':'5',
                              'min_qty':'0.00001','max_qty':'100','step_size':'0.00001','reserve_pct':'0.2'}}


class TriggerTests(unittest.TestCase):
    def test_edge_and_three_samples_to_rearm(self):
        s, c, x = {}, Settings(), snapshot()
        x['rolling']['15m']['priceChangePercent'] = -2.5
        self.assertEqual(len(evaluate(x,s,c,[],100)),1)
        s['pending'] = {}
        self.assertFalse(evaluate(x,s,c,[],200))
        x['rolling']['15m']['priceChangePercent'] = 0
        for t in [300,400,500]: evaluate(x,s,c,[],t)
        x['rolling']['15m']['priceChangePercent'] = -2.5
        self.assertEqual(len(evaluate(x,s,c,[],600)),1)

    def test_deeper_drawdown_bypasses_cooldown(self):
        s,c = {'last_success':100}, Settings()
        evaluate(snapshot(79000),s,c,[],100)
        s['pending']={}
        reasons=evaluate(snapshot(69000),s,c,[],200)
        self.assertTrue(any(r['key']=='drawdown_30' and r['urgent'] for r in reasons))
        self.assertTrue(should_analyze(s,reasons,c,200))

    def test_high_rolloff_does_not_rearm_drawdown(self):
        s,c={},Settings()
        evaluate(snapshot(79000),s,c,[],100)
        s['pending']={}
        for t in [200,300,400]: evaluate(snapshot(79000,90000),s,c,[],t)
        self.assertTrue(s['latches']['drawdown_20']['active'])
        self.assertFalse(evaluate(snapshot(71000,90000),s,c,[],500))

    def test_high_change_alone_no_trigger(self):
        s,c={},Settings()
        evaluate(snapshot(90000,100000),s,c,[],100)
        self.assertFalse(evaluate(snapshot(90000,110000),s,c,[],200))

    def test_due_failure_and_cooldown(self):
        c=Settings(); s={'last_success':100}
        r=[{'urgent':False}]
        self.assertFalse(should_analyze(s,r,c,200))
        self.assertFalse(should_analyze(s,[],c,21699))
        self.assertTrue(should_analyze(s,[],c,21700))
        s['retry_after']=22000
        self.assertFalse(should_analyze(s,[],c,21800))
        self.assertEqual(s['last_success'],100)

    def test_event_once_and_expired_pending_removed(self):
        from monitor.core import iso
        c,s=Settings(),{}
        e=[{'id':'cpi','name':'CPI','time_utc':iso(10000),'verified':True,'sources':['https://x.test']}]
        rs=evaluate(snapshot(),s,c,e,8500)
        self.assertTrue(rs[0]['urgent'])
        s['pending']={}
        self.assertFalse(evaluate(snapshot(),s,c,e,8600))
        self.assertTrue(evaluate(snapshot(),s,c,e,11000))
        self.assertFalse(evaluate(snapshot(),s,c,e,15000))

    def test_recovery_not_repeated_next_poll(self):
        s,c={},Settings(); x=snapshot(79000)
        evaluate(x,s,c,[],100); s['pending']={}
        x['daily'].update(rsi14_prev=29,rsi14=31)
        self.assertTrue(evaluate(x,s,c,[],200))
        s['pending']={}
        self.assertFalse(evaluate(x,s,c,[],300))


class IndicatorTests(unittest.TestCase):
    def test_rsi_extremes_flat_and_wilder(self):
        self.assertEqual(rsi(list(range(1,50))),100)
        self.assertEqual(rsi(list(range(50,1,-1))),0)
        self.assertEqual(rsi([100]*50),50)
        self.assertAlmostEqual(wilder([14]*14+[28]),15)

    def test_atr_true_range_includes_gap(self):
        rows=[{'open_ms':i,'close_ms':i,'open':100+i*10,'high':102+i*10,
               'low':98+i*10,'close':100+i*10,'volume':100} for i in range(250)]
        self.assertAlmostEqual(indicators(rows)['atr14'],12)


class AdviceTests(unittest.TestCase):
    def test_position_mismatch_and_pct_rejected(self):
        r=report(); validate(r,{'btc_quantity':0})
        with self.assertRaises(ValueError): validate(r,{'btc_quantity':1})
        r['reduce_pct']=101
        with self.assertRaises(ValueError): validate(r,{'btc_quantity':0})

    def test_buy_needs_current_action_and_range(self):
        r=report(); r.update(action='买入',buy_decision='买',buy_budget_pct=10)
        with self.assertRaises(ValueError): validate(r,{'btc_quantity':0})
        r.update(execution='现在',entry_low=80000,entry_high=81000)
        validate(r, funded_portfolio())

    def test_sell_quantity_uses_current_holdings(self):
        r=report(); r.update(action='减仓',position_mode='holding',buy_decision='不适用',reduce_pct=20,execution='现在')
        p={'btc_quantity':0.1}; validate(r,p)
        record={'analysis':r,'portfolio':p,'id':'x','snapshot_time':'x','completed_at':'x','valid_until':'x','reasons':[]}
        self.assertIn('对应卖出：0.02000000 BTC',render(record))
        self.assertEqual(p['btc_quantity'],0.1)

    def test_beijing_display_and_flat_fields(self):
        from monitor.core import display_time, localize_text
        self.assertEqual(display_time('2026-10-02T18:30:00Z'), '2026-10-03 02:30:00')
        self.assertEqual(display_time('2026-10-03T02:30:00+08:00'), '2026-10-03 02:30:00')
        self.assertIn('2026-10-03 02:30:00', localize_text('于2026-10-02T18:30:00Z'))
        r=report()
        record={'analysis':r,'portfolio':{'btc_quantity':0},'id':'x',
                'snapshot_time':'2026-10-02T18:30:00Z','completed_at':'2026-10-02T18:35:00Z',
                'valid_until':'2026-10-02T20:30:00Z','reasons':[], 'snapshot':snapshot(85000)}
        text=render(record)
        self.assertNotIn('对应卖出',text)
        self.assertNotIn('建议加仓',text)
        self.assertIn('85,000.00',text)
        self.assertIn('2026-10-03 04:30:00',text)
        self.assertNotIn('T18:30',text)


class FundingTests(unittest.TestCase):
    def test_usdc_cash_free_only_and_decimal_budget(self):
        from monitor.balance import parse_account, verified_portfolio
        from monitor.core import stamp
        from monitor.funding import budget_amount
        a=parse_account({'accountType':'SPOT','balances':[
            {'asset':'USDC','free':'100','locked':'900'},
            {'asset':'USDT','free':'99999','locked':'0'}]})
        p=verified_portfolio({'remaining_budget_usdt':99999},a,a,stamp(a['queried_at'])-1)
        self.assertEqual(str(budget_amount(p,10)), '10.00000000')
        with self.assertRaises(ValueError):
            verified_portfolio({},dict(a,usdc_free='99'),a,stamp(a['queried_at'])-1)

    def test_tiny_balance_and_unknown_rules_reject_purchase(self):
        from monitor.funding import check_purchase
        p=funded_portfolio(); p['usdc_free']='0.00675429'
        with self.assertRaises(ValueError): check_purchase(p,100)
        p['usdc_free']='100'; p['funding_rules']={'status':'unavailable'}
        with self.assertRaises(ValueError): check_purchase(p,100)

    def test_dip_requires_verified_cost_and_lower_entry(self):
        p=funded_portfolio(); p.update(btc_quantity=0.1,reference_price_usdt=80000)
        r=report(); r.update(action='补仓',position_mode='holding',buy_decision='不适用',
                            add_remaining_pct=10,execution='现在',entry_low=79000,entry_high=81000)
        with self.assertRaises(ValueError): validate(r,p)
        p['average_cost_usdt']=85000
        validate(r,p)
        p['average_cost_usdt']=80000
        with self.assertRaises(ValueError): validate(r,p)


class AnalysisTemporaryFilesTests(unittest.TestCase):
    def test_cleanup_after_success_and_error(self):
        from monitor.advisor import analyze
        paths=[]
        def worker(*args):
            work=args[-1]
            paths.append(work)
            self.assertTrue(work.is_dir())
            (work/'input.json').write_text('{}')
            if len(paths)==2:
                raise RuntimeError('simulated failure')
            return 'result'
        with patch('monitor.advisor._analyze',side_effect=worker):
            self.assertEqual(analyze(None,None,None,None,None,None,'test'), 'result')
            self.assertFalse(paths[0].exists())
            with self.assertRaises(RuntimeError):
                analyze(None,None,None,None,None,None,'test')
            self.assertFalse(paths[1].exists())


class BalanceTests(unittest.TestCase):
    def test_nonzero_locked_is_holding_and_local_zero_is_overridden(self):
        from monitor.balance import parse_account, verified_portfolio
        from monitor.core import stamp
        a=parse_account({'accountType':'SPOT','balances':[{'asset':'BTC','free':'0','locked':'0.125'}]})
        p=verified_portfolio({'btc_quantity':0,'planned_budget_usdt':100},a,a,stamp(a['queried_at'])-1)
        self.assertEqual(p['btc_quantity'],0.125)
        self.assertEqual(p['btc_free'],0)
        self.assertEqual(p['planned_budget_usdt'],100)
        self.assertIsNone(p['average_cost_usdt'])
        r=report(); r.update(balance=a,action='持有',position_mode='holding',buy_decision='不适用')
        validate(r,p)

    def test_invalid_or_failed_response_is_not_zero(self):
        from monitor.balance import parse_account
        for obj in [{}, {'code':-2015}, {'accountType':'SPOT','balances':None},
                    {'accountType':'SPOT','balances':[{'asset':'BTC','free':'NaN','locked':'0'}]}]:
            with self.assertRaises(ValueError): parse_account(obj)
        self.assertEqual(parse_account({'accountType':'SPOT','balances':[]})['btc_free'],'0')

    def test_changed_or_old_balance_rejected(self):
        from monitor.balance import parse_account, verified_portfolio
        from monitor.core import stamp
        a=parse_account({'accountType':'SPOT','balances':[]})
        old=dict(a,btc_free='0.1')
        with self.assertRaises(ValueError): verified_portfolio({},old,a,stamp(a['queried_at'])-1)
        old=dict(a,queried_at='2020-01-01T00:00:00Z')
        with self.assertRaises(ValueError): verified_portfolio({},old,a,stamp(a['queried_at'])-1)
        with self.assertRaises(ValueError): verified_portfolio({},dict(status='unavailable'),a,0)


class DeliveryTests(unittest.TestCase):
    def test_http_keeps_chinese_utf8_in_curl_configuration(self):
        # curl配置不识别JSON的unicode转义；外层必须保留真实UTF-8，否则中文变成uXXXX。
        c=Settings(); c.child_env=lambda: {}
        with patch('monitor.core.subprocess.run') as run:
            run.return_value=Mock(returncode=0,stdout='{"ok":true}\n200')
            Http(c).request('https://api.telegram.org/bot123:secret/sendMessage',{'text':'中文提醒😀'})
            call=run.call_args
            self.assertIn('中文提醒😀',call.kwargs['input'])
            self.assertNotIn('123:secret',' '.join(call.args[0]))

    def test_unicode_chunks_reconstruct(self):
        text='中😀'*5000
        parts=chunks(text)
        self.assertEqual(''.join(parts),text)
        self.assertTrue(all(len(x.encode('utf-16-le'))//2<=3000 for x in parts))

    def test_chunks_keep_sections_intact_when_they_fit(self):
        text='甲'*20+'\n\n'+'乙'*20+'\n\n'+'丙'*20
        self.assertEqual(chunks(text,45), ['甲'*20+'\n\n'+'乙'*20+'\n\n', '丙'*20])

    def test_retry_preserves_cursor_and_does_not_regenerate(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=Settings(); c.runtime=Path(tmp)
            out=Outbox(c); out.tg=Mock()
            out.enqueue('x','a'*6000,'2099-01-01T00:00:00Z')
            out.flush()
            p=c.runtime/'outbox/x.json'; item=read_json(p)
            self.assertEqual(item['next_part'],1)
            item['retry_at']=0; write_json(p,item)
            out.tg.send.side_effect=RuntimeError('offline')
            out.flush()
            self.assertEqual(read_json(p)['next_part'],1)
            out.tg.send.side_effect=None
            item=read_json(p); item['retry_at']=0; write_json(p,item)
            out.flush()
            self.assertEqual(read_json(p)['status'],'sent')

    def test_env_file_authoritative_and_child_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'.env').write_text('TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=55\n')
            with patch.dict('os.environ',{'TELEGRAM_BOT_TOKEN':'bad','OPENAI_API_KEY':'secret'}):
                c=Config(root)
                self.assertEqual(c.get('TELEGRAM_BOT_TOKEN'),'123:abc')
                self.assertNotIn('TELEGRAM_BOT_TOKEN',c.child_env())
                self.assertNotIn('OPENAI_API_KEY',c.child_env())
                self.assertNotIn('123:abc',c.redact('bot123:abc'))


if __name__=='__main__': unittest.main()
