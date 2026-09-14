"""角色工具白名单及确定性结果展示。所有归属来自 Request.user_id。"""
import re
from decimal import Decimal
from agents.tools import make_tool
from finance.store import money
from finance.policy import wants_handoff, operation_requested


def handoff_fields(req):
    # 本轮诉求是幂等基准；仅当“转人工”没有事件时，用同会话最近用户消息补齐。
    text = req.message
    users = [m['content'] for m in (req.history or [])
             if m.get('role')=='user' and m['content'].strip()!=text.strip()
             and m['content'].strip() not in ('转人工','转人工客服','请转人工','我要投诉','我要找人工')]
    if len(text) < 15 and users:
        text = users[-1]+'；'+text
    refs = re.findall(r'(?<![A-Za-z0-9])(?:TX|PAY)\d+(?![A-Za-z0-9])',text,re.I)
    dates = re.findall(r'\d{4}-\d{2}-\d{2}|昨天|今天|前天',text)
    desired = re.search(r'(?:希望|诉求[：:]?|期望)([^。；\n]{1,120})',text)
    return {'summary':text,'expected_resolution':desired.group(1).strip() if desired else '',
            'related_id':refs[0].upper() if refs else '', 'occurred_at':dates[0] if dates else ''}


def build_finance_tools(store, role):
    s=lambda description: {'type':'string','description':description}
    def product(req,args):
        ids=re.findall(r'EM\d{3}',req.message.upper())
        if ids and args['product_id'].upper() not in ids:
            return {'success':False,'error':'工具产品编号与本轮明确的产品不一致'}
        return store.product(args['product_id'])
    def create(req,args):
        if not (wants_handoff(req.message) or operation_requested(req.message)):
            return {'success':False,'error':'当前请求没有投诉/人工交接诉求，不能自行创建工单'}
        return store.create_ticket(req.user_id,req.conv_id,**handoff_fields(req))
    def update(req,args):
        # 更新必须有当前用户明确给出的工单号与字段值，历史内容不能自动更新。
        if args['ticket_id'] not in req.message or not any(w in req.message for w in ['补充','更新','希望','发生时间']):
            return {'success':False,'error':'请明确要补充的工单号、发生时间或期望处理方式'}
        for key in ('occurred_at','expected_resolution','details'):
            if args.get(key) and args[key] not in req.message:
                return {'success':False,'error':'补充内容必须由当前用户明确提供'}
        return store.update_ticket(args['ticket_id'],req.user_id,args)
    def fee(req,args):
        from finance.context import resolve_fee_context
        state=resolve_fee_context(req.message,req.history)
        if not state.ready:
            return {'success':False,'error':state.clarification()}
        supplied=dict(args,product_id=args['product_id'].upper(),amount=str(money(args['amount'])))
        keys=['product_id','action','amount','currency']+(['holding_days'] if state.slots['action']=='redeem' else [])
        if any(supplied.get(k)!=state.slots[k] for k in keys):
            return {'success':False,'error':'工具参数与当前费用咨询不一致，请使用最新明确的产品、操作和金额；不能混用历史参数'}
        calculation={k:state.slots[k] for k in keys}
        return dict(store.calculate_fee(**calculation),input_provenance={k:state.sources[k] for k in keys})

    def compare(req,args):
        if args['currency'] != 'CNY':
            raise ValueError('只支持同币种 CNY 金额比较')
        a,b=money(args['amount_a']),money(args['amount_b'])
        return {'success':True,'amount_a':str(a),'amount_b':str(b),'currency':'CNY','difference':str(a-b),
                'notice':'仅为输入金额差，不代表重复扣款或已批准退费'}
    all_tools = {
        'list_products':make_tool('list_products','列出模拟产品目录，不给适当性或购买结论',{},lambda req,args:{'success':True,'simulation':True,'products':store.products}),
        'get_product':make_tool('get_product','读取模拟产品费率、风险和申赎规则（含版本及时间）',{'product_id':s('产品编号 EM001/EM002/EM003')},product,['product_id']),
        'calculate_fee':make_tool('calculate_fee','确定性申赎费用试算；必须使用明确给出的金额，不推断持仓；不执行资金操作',{'product_id':s('产品编号'),'action':{'type':'string','enum':['subscribe','redeem']},'amount':s('十进制金额，例如 10000.00，不能输入份额'),'currency':{'type':'string','enum':['CNY']},'holding_days':{'type':'integer','minimum':0,'maximum':36500}},fee,['product_id','action','amount','currency']),
        'get_transaction':make_tool('get_transaction','查询当前客户所属的模拟申赎记录；必须重新查询，不沿用历史状态',{'transaction_id':s('TX 开头业务编号')},lambda req,args:store.lookup('transactions',args['transaction_id'].upper(),req.user_id),['transaction_id']),
        'get_payment':make_tool('get_payment','查询当前客户所属的模拟支付扣费记录',{'payment_id':s('PAY 开头支付编号')},lambda req,args:store.lookup('payments',args['payment_id'].upper(),req.user_id),['payment_id']),
        'get_account':make_tool('get_account','查询当前客户的模拟账户和正式风险测评记录；聊天偏好不改变测评',{},lambda req,args:store.lookup('accounts',req.user_id,req.user_id)),
        'compare_amounts':make_tool('compare_amounts','比较同币种 CNY 的两笔明确金额，不执行退费',{'amount_a':s('金额字符串'),'amount_b':s('金额字符串'),'currency':{'type':'string','enum':['CNY']}},compare,['amount_a','amount_b','currency']),
        'create_service_ticket':make_tool('create_service_ticket','为当前请求创建本地模拟工单；摘要和身份由服务端生成，返回真实编号；没有真实人工通知',{},create),
        'get_service_ticket':make_tool('get_service_ticket','查询当前客户所属工单',{'ticket_id':s('SR 开头工单编号')},lambda req,args:store.get_ticket(args['ticket_id'].upper(),req.user_id),['ticket_id']),
        'update_service_ticket':make_tool('update_service_ticket','补充自己工单的发生时间及期望；不能改变状态为已批准',{'ticket_id':s('明确工单编号'),'occurred_at':s('用户明确给出的发生时间'),'expected_resolution':s('用户明确给出的期望处理方式'),'details':s('用户明确给出的具体问题补充')},update,['ticket_id']),
    }
    scopes={
      'general':['list_products','get_product','get_account','get_service_ticket','update_service_ticket'],
      'technical':['get_account'],
      'billing':['get_payment','get_transaction','compare_amounts','get_service_ticket'],
      'product':['list_products','get_product','calculate_fee','get_transaction','get_account'],
      'escalation':['create_service_ticket','get_service_ticket','update_service_ticket','get_payment','get_transaction'],
    }
    return {k:all_tools[k] for k in scopes[role]}


def render_result(name,result):
    if not result.get('success',True):
        return '查询/处理未完成：'+str(result.get('error','未取得有效结果'))
    if 'ticket' in result:
        t=result['ticket']
        return (f"模拟服务工单：{t['ticket_id']}\n状态：{t['status']}"+
                ('（复用已有工单，未重复落单）' if result.get('deduplicated') else '')+
                f"\n诉求摘要：{t['summary']}\n期望：{t['expected_resolution'] or '待补充'}\n发生时间：{t['occurred_at'] or '待补充'}\n待补充：{'、'.join(t['missing_fields']) or '无'}\n{t['next_step']}")
    if name=='calculate_fee':
        return (f"{result['product_id']} {'赎回' if result['action']=='redeem' else '申购'}费用试算：\n"
                f"金额 {result['amount']} {result['currency']}，费率 {Decimal(result['rate'])*100}%，费用 {result['fee']} {result['currency']}，净额 {result['net_amount']} {result['currency']}。\n"
                f"公式：{result['formula']}\n精度：{result['rounding']}\n依据：{result['source']} / {result['version']}；适用 {result['effective_from']} 至 {result['effective_to']}。\n{result['notice']}")
    if name=='compare_amounts':
        return f"{result['amount_a']} − {result['amount_b']} = {result['difference']} {result['currency']}。{result['notice']}"
    if 'record' in result:
        r=result['record']
        if name=='get_account':
            risk=r.get('risk_assessment')
            return f"模拟账户：{r['account_id']}，状态：{r['status']}。\n正式测评记录："+(f"{risk['level']}，测评 {risk['assessed_at']}，有效至 {risk['expires_at']}。" if risk else '尚无记录，请通过正式流程完成测评。')+'聊天偏好不能改变此记录。'
        lines=[f"模拟业务记录：{r.get('transaction_id') if name=='get_transaction' else r.get('payment_id')}",f"状态：{r['status']}",f"金额：{r['amount']} {r['currency']}",f"业务快照时间：{r['as_of']}（非实时）"]
        if r.get('estimated_settlement'): lines.append('预计到账：'+r['estimated_settlement']+'，不保证实际到账时间。')
        if r.get('note'): lines.append(r['note'])
        return '\n'.join(lines)
    if 'product' in result:
        p=result['product']
        return f"{p['product_id']} {p['name']}\n类型：{p['product_type']}；风险：{p['risk_level']}。{p['description']}\n开放：{p['opening']}\n确认：{p['confirmation']}\n到账：{p['settlement']}\n申购费率：{Decimal(p['subscription_rate'])*100}%；赎回费率："+'；'.join(f"≥{r['min_days']}天：{Decimal(r['rate'])*100}%" for r in p['redemption_rates'])+f"。\n管理/托管/销售服务年费率：{Decimal(p['management_rate'])*100}% / {Decimal(p['custody_rate'])*100}% / {Decimal(p['sales_service_rate'])*100}%（净值中计提）。\n来源：{p['source']} / {p['version']}，适用 {p['effective_from']} 至 {p['effective_to']}。"
    if 'products' in result:
        return '模拟产品目录（请提供编号继续咨询）：\n'+'\n'.join(f"{p['product_id']} {p['name']} — {p['product_type']} / {p['risk_level']}" for p in result['products'])
    if 'results' in result:
        return '\n\n'.join(f"{d['title']}：{d['content']}\n依据：{d.get('source','待核实')} / {d.get('source_type','unknown')} / {d.get('version','unknown')}；适用 {d.get('effective_from','未知')} 至 {d.get('effective_to','未知')}" for d in result['results']) or '没有检索到足够依据，请补充产品编号或具体问题。'
    if name=='lookup_error_code':
        return f"{result['error_code']}：{result['meaning']}。\n"+'\n'.join(result['next_steps'])+'\n未读取后台日志。'
    return '请提供具体产品或业务编号，以便进一步核验。'
