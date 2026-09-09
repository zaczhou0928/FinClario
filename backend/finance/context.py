"""从同会话用户输入恢复有限的费用咨询状态；不从助手回复提取业务事实。"""
import re
from dataclasses import dataclass, field
from finance.store import money

FEE_TOPIC = re.compile(r'手续费|费率|试算|申购费用|赎回费用')
OTHER_TOPIC = re.compile(r'登录|验证码|投诉|人工|工单|扣款|支付|测评|账户|进度|到账|TX\d|PAY\d|SR[A-Z0-9]|取消试算|不用算|不算了', re.I)


@dataclass
class FeeContext:
    active: bool = False
    slots: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)
    continuation: bool = False

    @property
    def missing(self):
        required = ['product_id', 'action', 'amount', 'currency']
        if self.slots.get('action') == 'redeem':
            required.append('holding_days')
        return [k for k in required if k not in self.slots]

    @property
    def ready(self):
        return self.active and not self.missing and not self.errors

    def clarification(self):
        labels = {'product_id':'产品编号', 'action':'申购还是赎回', 'amount':'明确的金额（不是份额）',
                  'currency':'币种 CNY', 'holding_days':'持有天数'}
        reasons = list(self.errors.values())
        if self.missing:
            reasons.append('请补充：'+'、'.join(labels[k] for k in self.missing)+'。')
        return '费用试算尚未完成。'+''.join(reasons)


def extract_fields(text):
    fields = {}
    ids = set(re.findall(r'(?<![A-Za-z0-9])EM\d{3}(?![A-Za-z0-9])', text, re.I))
    if ids: fields['product_id'] = {v.upper() for v in ids}
    actions = {action for word, action in [('申购','subscribe'),('赎回','redeem')] if word in text}
    if actions: fields['action'] = actions
    amounts = re.findall(r'(?<![\d.])(-?\d+(?:,\d{3})*(?:\.\d+)?)\s*(元|CNY|人民币|USD|美元|EUR)', text, re.I)
    if amounts:
        fields['amount'] = {v.replace(',','') for v,c in amounts}
        fields['currency'] = {'CNY' if c.upper() in ('元','CNY','人民币') else 'USD' if c=='美元' else c.upper() for v,c in amounts}
    days = re.findall(r'(?:持有|持仓)(?:了)?\s*(-?\d+)\s*天', text)
    if days: fields['holding_days'] = {int(v) for v in days}
    return fields


def resolve_fee_context(message, history=None):
    state = FeeContext()
    users = [m['content'] for m in (history or []) if m.get('role')=='user'] + [message]
    for turn, text in enumerate(users):
        fields = extract_fields(text)
        explicit = bool(FEE_TOPIC.search(text))
        continuation = state.active and bool(fields) and not OTHER_TOPIC.search(text)
        if not explicit and not continuation:
            state = FeeContext()
            continue
        if not state.active:
            state = FeeContext(active=True)
        # 切换产品/操作时不自动继承旧金额和天数，当前轮明确给出的值可以重新填入。
        changed = any(k in fields and fields[k] != {state.slots[k]} for k in ('product_id','action') if k in state.slots)
        if changed:
            for k in ('amount','currency','holding_days'):
                state.slots.pop(k,None); state.sources.pop(k,None); state.errors.pop(k,None)
        for key, values in fields.items():
            state.slots.pop(key,None); state.sources.pop(key,None); state.errors.pop(key,None)
            if len(values) != 1:
                state.errors[key] = '检测到多个候选值，请一次明确一组产品、金额和操作。'
                continue
            value = next(iter(values))
            try:
                if key=='amount':
                    value = str(money(value))
                    if money(value)<=0: raise ValueError('金额必须大于零')
                if key=='currency' and value!='CNY': raise ValueError('仅支持明确的 CNY 金额，不自动换汇')
                if key=='holding_days' and not 0<=value<=36500: raise ValueError('持有天数须为 0—36500 的整数')
            except ValueError as ex:
                state.errors[key] = str(ex)+'。'
                continue
            state.slots[key] = value
            state.sources[key] = {'user_turn':turn, 'current_turn':turn==len(users)-1}
        state.continuation = bool(continuation and not explicit)
    return state
