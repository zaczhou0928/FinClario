"""无密钥演示计划器：调用与模型模式同一白名单工具，不冒充 LLM。"""
import re
from finance.tools import render_result
from finance.policy import wants_handoff


def plan(req, role):
    text=req.message
    # 历史仅用于省略产品指代；业务状态始终重新查工具。
    ids=re.findall(r'EM\d{3}',text,re.I)
    if not ids:
        for m in reversed(req.history or []):
            if m.get('role')=='user':
                ids=re.findall(r'EM\d{3}',m['content'],re.I)
                if ids: break
    product=ids[0].upper() if ids else ''
    tx=re.search(r'(?<![A-Za-z0-9])TX\d+(?![A-Za-z0-9])',text,re.I)
    pay=re.search(r'(?<![A-Za-z0-9])PAY\d+(?![A-Za-z0-9])',text,re.I)
    ticket=re.search(r'(?<![A-Za-z0-9])SR[A-Z0-9]{6,}(?![A-Za-z0-9])',text,re.I)
    calls=[]; extra=''
    from finance.context import resolve_fee_context
    fee=resolve_fee_context(text,req.history)
    if role=='product' and fee.active:
        if fee.ready:
            args={k:v for k,v in fee.slots.items() if k!='holding_days' or fee.slots['action']=='redeem'}
            return [('calculate_fee',args)],''
        if fee.slots.get('product_id'):
            calls.append(('get_product',{'product_id':fee.slots['product_id']}))
        else:
            calls.append(('search_knowledge_base',{'query':'申购赎回费用试算','top_k':1}))
        return calls,fee.clarification()
    if ticket and role in ('escalation','general','billing'):
        if any(w in text for w in ['补充','更新','希望','发生时间']) and role!='billing':
            args={'ticket_id':ticket[0].upper()}
            dt=re.search(r'\d{4}-\d{2}-\d{2}|昨天|今天|前天',text)
            wanted=re.search(r'(?:希望|期望)([^。；\n]+)',text)
            if dt: args['occurred_at']=dt[0]
            if wanted: args['expected_resolution']=wanted[1].strip()
            detail=re.search(r'问题[：:](.*?)(?:，希望|，发生时间|$)',text)
            if detail: args['details']=detail[1].strip()
            calls.append(('update_service_ticket',args))
        else: calls.append(('get_service_ticket',{'ticket_id':ticket[0].upper()}))
    elif role=='escalation':
        calls.append(('create_service_ticket',{}))
    elif any(w in text for w in ['投诉','工单']) and not wants_handoff(text) and not any(w in text for w in ['到账','赎回','申购']):
        calls.append(('search_knowledge_base',{'query':'投诉 人工交接 工单 发生时间 期望处理','top_k':1}))
        extra='如需创建工单，请明确提出投诉或转人工，并提供问题、发生时间和期望处理方式；流程咨询不会创建工单。'
    elif role=='technical':
        code=re.search(r'(?<![A-Za-z0-9])[45]\d{2}(?![A-Za-z0-9])',text)
        if code: calls.append(('lookup_error_code',{'error_code':code[0]}))
        calls.append(('search_knowledge_base',{'query':'登录 验证码 页面故障','top_k':1}))
    elif role=='billing':
        if pay: calls.append(('get_payment',{'payment_id':pay[0].upper()}))
        elif tx: calls.append(('get_transaction',{'transaction_id':tx[0].upper()}))
        else:
            calls.append(('search_knowledge_base',{'query':'支付扣费争议处理','top_k':1}))
            extra='请提供模拟支付编号（例如 PAY1001）及发生时间，不要发送完整卡号或验证码。'
    elif tx:
        calls.append(('get_transaction',{'transaction_id':tx[0].upper()}))
    elif any(w in text for w in ['测评','评估','账户状态']):
        calls.append(('get_account',{}))
    elif product:
        calls.append(('get_product',{'product_id':product}))
    elif any(w in text for w in ['产品','基金类型','有哪些基金']):
        calls.append(('list_products',{}))
    elif any(w in text for w in ['费用','手续费','费率']):
        calls.append(('search_knowledge_base',{'query':'申购赎回费用试算','top_k':1}))
        extra='请提供产品编号、金额与币种；赎回还需持有天数。全部赎回的费用咨询不需要转人工。'
    elif any(w in text for w in ['到账','确认','开放日']):
        calls.append(('search_knowledge_base',{'query':'模拟开放日、确认和到账','top_k':1}))
        extra='具体到账安排需要产品编号；已有申请请提供业务编号后查询，聊天描述不能替代最新记录。'
    elif any(w in text for w in ['收益','风险','波动','流动性']):
        calls.append(('search_knowledge_base',{'query':'基金风险 历史收益 波动 流动性','top_k':1}))
    elif any(w in text for w in ['你好','您好','谢谢']):
        extra='您好，我是 FinClario 金融客服。可咨询模拟基金产品、申赎规则、支付账户问题，也可查询业务进度或提交投诉工单。'
    else:
        calls.append(('search_knowledge_base',{'query':text,'top_k':2}))
        extra='可补充产品编号（EM001—EM003）、业务编号或具体问题，以便进一步说明。'
    return calls,extra
