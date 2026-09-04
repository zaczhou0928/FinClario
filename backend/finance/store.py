"""本地模拟业务账本。身份由 API 会话注入，模型不能指定记录归属。"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / 'data' / 'finance'
CENT = Decimal('0.01')


def now():
    return datetime.now(timezone.utc).isoformat()


def sanitize(text):
    text = re.sub(r'(?<!\d)\d{11,19}(?!\d)', '[已隐去长号码]', str(text))
    return re.sub(r'(密码|验证码|口令)\s*[:：是]?\s*[A-Za-z0-9]{4,}', r'\1[已隐去]', text)


def money(value):
    if isinstance(value, (bool, float)):
        raise ValueError('金额请使用十进制字符串，最多两位小数')
    raw = str(value)
    if not re.fullmatch(r'\d{1,12}(?:\.\d{1,2})?', raw):
        raise ValueError('金额必须为非负十进制字符串，最多两位小数')
    try:
        result = Decimal(raw)
        if not result.is_finite():
            raise ValueError('金额必须是有限数')
        return result.quantize(CENT)
    except InvalidOperation as ex:
        raise ValueError('无效金额') from ex


class FinanceStore:
    def __init__(self, path, as_of=None):
        self.path = str(path)
        self.as_of = date.fromisoformat(as_of).isoformat() if as_of else None
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.products = json.loads((DATA_DIR / 'products.json').read_text())
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS records(kind TEXT, id TEXT, user_id TEXT, data TEXT,
                    PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS tickets(id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    conv_id TEXT NOT NULL, fingerprint TEXT NOT NULL, data TEXT NOT NULL,
                    UNIQUE(user_id,conv_id,fingerprint));
                CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, user_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chat_requests(user_id TEXT, id TEXT, fingerprint TEXT,
                    conv_id TEXT, response TEXT, PRIMARY KEY(user_id,id));
            ''')
            records = json.loads((DATA_DIR / 'records.json').read_text())
            for kind, rows in records.items():
                key = {'accounts':'user_id','transactions':'transaction_id','payments':'payment_id'}[kind]
                for row in rows:
                    db.execute('INSERT OR IGNORE INTO records VALUES (?,?,?,?)',
                               (kind, row[key], row['user_id'], json.dumps(row, ensure_ascii=False)))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def bind_conversation(self, user_id, conv_id):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO conversations VALUES (?,?)', (conv_id,user_id))
            owner = db.execute('SELECT user_id FROM conversations WHERE id=?', (conv_id,)).fetchone()
            if owner['user_id'] != user_id:
                raise PermissionError('会话不存在或无权访问')

    def claim_request(self, user_id, request_id, fingerprint, conv_id):
        """数据库唯一约束保护网络重试；并发重复请求返回进行中，完成后复用响应。"""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM chat_requests WHERE user_id=? AND id=?', (user_id,request_id)).fetchone()
            if row:
                if row['fingerprint'] != fingerprint:
                    raise ValueError('同一 request_id 不能用于不同请求')
                if row['response'] is None:
                    raise ValueError('相同请求正在处理，请稍后使用原 request_id 重试')
                return json.loads(row['response'])
            db.execute('INSERT INTO chat_requests VALUES (?,?,?,?,NULL)', (user_id,request_id,fingerprint,conv_id))
        return None

    def finish_request(self, user_id, request_id, response):
        with self.connect() as db:
            db.execute('UPDATE chat_requests SET response=? WHERE user_id=? AND id=?',
                       (json.dumps(response, ensure_ascii=False),user_id,request_id))

    def release_request(self, user_id, request_id):
        with self.connect() as db:
            db.execute('DELETE FROM chat_requests WHERE user_id=? AND id=? AND response IS NULL', (user_id,request_id))

    def lookup(self, kind, record_id, user_id):
        with self.connect() as db:
            row = db.execute('SELECT data FROM records WHERE kind=? AND id=? AND user_id=?',
                             (kind,record_id,user_id)).fetchone()
        if not row:
            return {'success':False,'error':'记录不存在或无权访问','simulation':True}
        return {'success':True,'simulation':True,'record':json.loads(row['data']), 'queried_at':now()}

    def list_records(self, kind, user_id):
        with self.connect() as db:
            rows = db.execute('SELECT data FROM records WHERE kind=? AND user_id=? ORDER BY id', (kind,user_id)).fetchall()
        return [json.loads(r['data']) for r in rows]

    def product(self, product_id):
        p = next((p for p in self.products if p['product_id']==product_id.upper()), None)
        if not p:
            return {'success':False,'error':'产品不存在，请选择 EM001、EM002 或 EM003'}
        today = self.as_of or date.today().isoformat()
        if not p['effective_from'] <= today <= p['effective_to']:
            return {'success':False,'error':'产品资料不在适用时间内，需核实新版本'}
        return {'success':True,'simulation':True,'product':dict(p)}

    def calculate_fee(self, product_id, action, amount, currency, holding_days=None):
        if currency != 'CNY':
            raise ValueError('模拟产品仅支持 CNY，不进行汇率转换')
        if action not in ('subscribe','redeem'):
            raise ValueError('仅支持申购或赎回费用试算')
        result = self.product(product_id)
        if not result['success']:
            return result
        p = result['product']
        gross = money(amount)
        if gross <= 0:
            raise ValueError('试算金额必须大于零')
        if action == 'redeem':
            if type(holding_days) is not int or not 0 <= holding_days <= 36500:
                raise ValueError('赎回必须提供 0—36500 的整数持有天数')
            rate = Decimal(max((r for r in p['redemption_rates'] if holding_days >= r['min_days']), key=lambda r:r['min_days'])['rate'])
            fee = (gross * rate).quantize(CENT, rounding=ROUND_HALF_UP)
            net = gross - fee
            formula = '费用 = 确认/假设赎回金额 × 持有天数档位费率；净额 = 金额 − 费用'
        else:
            rate = Decimal(p['subscription_rate'])
            net = (gross / (1+rate)).quantize(CENT, rounding=ROUND_HALF_UP)
            fee = gross - net
            formula = '净申购金额 = 申购金额 ÷ (1 + 申购费率)；费用 = 申购金额 − 净金额'
        return {'success':True,'simulation':True,'product_id':product_id,'action':action,'currency':currency,
                'amount':str(gross),'rate':str(rate),'fee':str(fee),'net_amount':str(net),'holding_days':holding_days,
                'formula':formula,'rounding':'ROUND_HALF_UP，金额保留两位小数','version':p['version'],
                'source':p['source'],'effective_from':p['effective_from'],'effective_to':p['effective_to'],
                'notice':'仅按输入的假设/确认金额试算，不代表实时净值、持仓或实际到账；未执行申赎。'}

    def create_ticket(self, user_id, conv_id, summary, expected_resolution, related_id='', occurred_at=''):
        summary, expected_resolution = sanitize(summary).strip()[:1200], sanitize(expected_resolution).strip()[:300]
        if not summary:
            raise ValueError('请补充需要处理的具体问题')
        if related_id:
            kind = 'transactions' if related_id.startswith('TX') else 'payments' if related_id.startswith('PAY') else None
            if kind is None or not self.lookup(kind,related_id,user_id)['success']:
                return {'success':False,'error':'关联业务记录不存在或无权访问'}
        # Exact normalized content protects retries even when the model changes its summary.
        fingerprint = hashlib.sha256(re.sub(r'\s+','', summary+'|'+expected_resolution+'|'+related_id).encode()).hexdigest()
        missing = [k for k,v in [('发生时间',occurred_at),('期望处理方式',expected_resolution)] if not v]
        if summary in ('我要投诉','转人工','转人工客服','我要找人工','请转人工'):
            missing.insert(0,'具体问题')
        ticket = {'ticket_id':'SR'+uuid.uuid4().hex[:12].upper(),'summary':summary,
                  'expected_resolution':expected_resolution,'related_id':related_id,'occurred_at':sanitize(occurred_at)[:100],
                  'status':'待补充信息' if missing else '待人工核验','missing_fields':missing,
                  'created_at':now(),'updated_at':now(),'simulation':True,
                  'next_step':'可在本会话补充信息，或凭工单号查询。仅写入本地模拟队列，未通知真实客服；未批准补偿、退费或任何资金操作。'}
        with self.connect() as db:
            changed = db.execute('INSERT OR IGNORE INTO tickets VALUES (?,?,?,?,?)',
                                (ticket['ticket_id'],user_id,conv_id,fingerprint,json.dumps(ticket,ensure_ascii=False))).rowcount
            row = db.execute('SELECT data FROM tickets WHERE user_id=? AND conv_id=? AND fingerprint=?',
                             (user_id,conv_id,fingerprint)).fetchone()
        return {'success':True,'simulation':True,'deduplicated':not bool(changed),'ticket':json.loads(row['data'])}

    def get_ticket(self, ticket_id, user_id):
        with self.connect() as db:
            row = db.execute('SELECT data FROM tickets WHERE id=? AND user_id=?', (ticket_id,user_id)).fetchone()
        return {'success':True,'simulation':True,'ticket':json.loads(row['data'])} if row else {'success':False,'error':'工单不存在或无权访问'}

    def update_ticket(self, ticket_id, user_id, details):
        with self.connect() as db:
            row = db.execute('SELECT data FROM tickets WHERE id=? AND user_id=?',(ticket_id,user_id)).fetchone()
            if not row:
                return {'success':False,'error':'工单不存在或无权访问'}
            ticket = json.loads(row['data'])
            if details.get('details'):
                ticket['summary'] += '；补充：'+sanitize(details['details'])[:600]
            for key in ('occurred_at','expected_resolution'):
                if details.get(key):
                    ticket[key] = sanitize(details[key])[:300]
            ticket['missing_fields'] = [k for k,v in [('发生时间',ticket['occurred_at']),('期望处理方式',ticket['expected_resolution'])] if not v]
            if ticket['summary'] in ('我要投诉','转人工','转人工客服','我要找人工','请转人工'):
                ticket['missing_fields'].insert(0,'具体问题')
            ticket['status'] = '待补充信息' if ticket['missing_fields'] else '待人工核验'
            ticket['updated_at'] = now()
            db.execute('UPDATE tickets SET data=? WHERE id=? AND user_id=?', (json.dumps(ticket,ensure_ascii=False),ticket_id,user_id))
        return {'success':True,'simulation':True,'ticket':ticket}

    def list_tickets(self, user_id):
        with self.connect() as db:
            rows = db.execute('SELECT data FROM tickets WHERE user_id=? ORDER BY rowid DESC LIMIT 30',(user_id,)).fetchall()
        return [json.loads(r['data']) for r in rows]
