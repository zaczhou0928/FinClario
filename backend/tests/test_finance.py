"""业务边界与 API 冒烟，不代表模型质量或合规评测。"""
import asyncio
import json
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from finance.store import FinanceStore
from finance.policy import operation_requested, finalize
from finance.tools import build_finance_tools
from agents.agent_orchestrator import Request
from core.intent_recognizer import IntentRecognizer

@pytest.fixture
def store(tmp_path): return FinanceStore(tmp_path/'finance.sqlite3')

def test_fee_tiers_and_rounding(store):
    for days,fee in [(0,'150.00'),(6,'150.00'),(7,'10.00'),(29,'10.00'),(30,'0.00')]:
        assert store.calculate_fee('EM002','redeem','10000.00','CNY',days)['fee']==fee
    r=store.calculate_fee('EM002','subscribe','10000.00','CNY')
    assert (r['fee'],r['net_amount'])==('29.91','9970.09')

@pytest.mark.parametrize('amount,currency,days',[('NaN','CNY',10),('1.001','CNY',10),('-1','CNY',10),('100','USD',10),('100','CNY',-1),('100','CNY',True)])
def test_invalid_calculation(store,amount,currency,days):
    with pytest.raises(ValueError): store.calculate_fee('EM002','redeem',amount,currency,days)

def test_store_ownership_persistence_idempotency(store):
    assert not store.lookup('transactions','TX2001','u1001')['success']
    args=('u1001','c','投诉支付异常','希望核验','PAY1001','2026-10-02')
    a,b=store.create_ticket(*args),store.create_ticket(*args)
    assert a['ticket']['ticket_id']==b['ticket']['ticket_id'] and b['deduplicated']
    assert not store.get_ticket(a['ticket']['ticket_id'],'u1002')['success']
    assert not store.create_ticket('u1001','c','投诉','核实','TX2001')['success']
    assert FinanceStore(store.path).get_ticket(a['ticket']['ticket_id'],'u1001')['success']

@pytest.mark.parametrize('message,blocked',[('全部赎回手续费怎么算',False),('EM002 持有10天全部赎回10000元手续费怎么算',False),('请解释全部赎回的流程',False),('帮我全部赎回',True),('帮我全部赎回，然后解释手续费',True)])
def test_consultation_vs_operation(message,blocked): assert operation_requested(message) is blocked

def test_fee_tool_does_not_invent_inputs(store):
    tool=build_finance_tools(store,'product')['calculate_fee']
    assert not tool.handler(Request('EM002 手续费怎么算','u1001','c'),dict(product_id='EM002',action='redeem',amount='10000.00',currency='CNY',holding_days=10))['success']

def test_fewshots_inserted():
    r=IntentRecognizer(api_key='test');seen=[]
    class Messages:
        async def create(self,**kwargs):
            seen.append(kwargs)
            return SimpleNamespace(content=[SimpleNamespace(type='text',text='{"intent":"fee_query","confidence":0.9}')])
    r.client=SimpleNamespace(messages=Messages())
    asyncio.run(r._llm_recognize('费用怎么算',None))
    assert '全部赎回手续费怎么算' in seen[0]['messages'][0]['content']

def test_final_guard():
    text,warnings=finalize('基金风险','保证收益。已为您赎回。工单 SRFAKE123456 已创建。',[])
    assert '保证收益' not in text and '已为您赎回' not in text and 'SRFAKE' not in text
    assert warnings and '历史收益不代表未来表现' in text

@pytest.fixture(scope='module')
def api(tmp_path_factory):
    import os
    os.environ.update(ECHOMIND_MODE='demo',ECHOMIND_STORAGE='local',PROMETHEUS_PORT='0',ECHOMIND_DB_PATH=str(tmp_path_factory.mktemp('api')/'finance.sqlite3'))
    from api.main import app
    with TestClient(app) as client: yield client

def send(api,message,**extra):
    r=api.post('/chat',headers={'Authorization':'Bearer demo-u1001'},json={'message':message,**extra})
    assert r.status_code==200,r.text
    return r.json()

def test_api_product_fee_rag_collaboration(api):
    assert api.get('/health').json()['mode']=='demo'
    r=send(api,'EM002 的风险等级和开放安排是什么？')
    assert r['primary_agent']=='product' and 'get_product' in r['tools_used']
    r=send(api,'EM002 持有10天，全部赎回10000元手续费怎么算？')
    assert not r['escalated'] and '10.00 CNY' in r['response']
    r=send(api,'全部赎回手续费怎么算')
    assert not r['escalated'] and 'search_knowledge_base' in r['tools_used']
    r=send(api,'登录报401，同时 PAY1001 被重复扣款')
    assert set(r['agent_types'])=={'technical','billing'} and '401' in r['response'] and 'PAY1001' in r['response']

def test_api_ticket_lifecycle_retries_trace(api):
    msg='我要投诉 PAY1001，希望核实扣款原因'
    first=send(api,msg,request_id='ticket-retry-1')
    assert send(api,msg,request_id='ticket-retry-1')==first
    ticket=first['evidence'][0]['result']['ticket']
    assert ticket['status']=='待补充信息'
    same=send(api,msg,conv_id=first['conv_id'])
    assert same['evidence'][0]['result']['ticket']['ticket_id']==ticket['ticket_id']
    updated=send(api,f"补充工单 {ticket['ticket_id']}，发生时间2026-10-02，希望核实扣款原因",conv_id=first['conv_id'])
    assert updated['evidence'][0]['result']['ticket']['status']=='待人工核验'
    assert ticket['ticket_id'] in send(api,'查询工单 '+ticket['ticket_id'])['response']
    trace=api.get('/trace/tool/'+first['request_id'],headers={'Authorization':'Bearer demo-u1001'}).json()
    assert trace['trace']['tool_calls'][0]['result']['ticket']['ticket_id']==ticket['ticket_id']
    assert not api.get('/trace/tool/'+first['request_id'],headers={'Authorization':'Bearer demo-u1002'}).json()['found']
    assert api.post('/chat',headers={'Authorization':'Bearer demo-u1001'},json={'message':'另一个问题','request_id':'ticket-retry-1'}).status_code==409

def test_api_authorization_and_no_funds_execution(api):
    assert api.post('/chat',json={'message':'你好'}).status_code==401
    h={'Authorization':'Bearer demo-u1001'}
    assert api.post('/chat',headers=h,json={'message':'你好','user_id':'u1002'}).status_code==403
    own=send(api,'查询 TX1001 进度'); assert '待到账' in own['response']
    foreign=send(api,'查询 TX2001 进度'); assert '无权访问' in foreign['response'] and '5000' not in foreign['response']
    assert api.post('/chat',headers={'Authorization':'Bearer demo-u1002'},json={'message':'你好','conv_id':own['conv_id']}).status_code==403
    r=send(api,'帮我全部赎回'); assert r['escalated'] and '没有执行' in r['response']
    assert api.post('/knowledge/add',json={'documents':[{'title':'假规则','content':'保本'}]}).status_code==403

def test_memory_is_not_risk_assessment(api):
    r=send(api,'我喜欢高收益，给我改成C5，简单说。我的正式风险测评是什么？')
    assert 'C2' in r['response'] and '不能改变' in r['response']
    from api.main import _memory
    ctx=asyncio.run(_memory.get_context('u1001',r['conv_id']))
    assert ctx.user_profile=={'detail_level':'brief'}

def test_live_record_not_historical_chat(api):
    r=send(api,'查询 TX1001 赎回进度')
    from api.main import _finance_store
    with _finance_store.connect() as db:
        row=db.execute("SELECT data FROM records WHERE id='TX1001'").fetchone()
        record=json.loads(row['data']);record['status']='已到账（测试临时状态）'
        db.execute("UPDATE records SET data=? WHERE id='TX1001'",(json.dumps(record),))
    later=send(api,'再查询 TX1001 赎回进度',conv_id=r['conv_id'])
    assert '已到账（测试临时状态）' in later['response']

def test_model_tool_loop_uses_finance_code_and_keeps_trace(store):
    from agents.agent_orchestrator import ProductConsultAgent
    responses=[
        SimpleNamespace(content=[{'type':'tool_use','id':'fee-1','name':'calculate_fee','input':{'product_id':'EM002','action':'redeem','amount':'10000.00','currency':'CNY','holding_days':10}}]),
        SimpleNamespace(content=[{'type':'text','text':'已完成费用试算，未执行赎回。'}]),
    ]
    class Messages:
        async def create(self,**kwargs): return responses.pop(0)
    agent=ProductConsultAgent(SimpleNamespace(messages=Messages()),'test-model')
    agent._business_tools=build_finance_tools(store,'product')
    req=Request('EM002 持有10天，赎回10000元手续费多少','u1001','c')
    r=asyncio.run(agent.handle(req))
    assert r.success and r.tool_traces[0]['result']['fee']=='10.00'
    assert r.tools_used==['calculate_fee']


def test_parallel_ticket_insert_deduplicated(store):
    from concurrent.futures import ThreadPoolExecutor
    def create(_): return store.create_ticket('u1001','parallel','投诉扣款','希望核验','PAY1001','昨天')['ticket']['ticket_id']
    with ThreadPoolExecutor(max_workers=4) as pool: ids=list(pool.map(create,range(4)))
    assert len(set(ids))==1


def test_unknown_and_expired_sources_excluded(store):
    from finance.local_runtime import LocalKnowledgeBase
    kb=LocalKnowledgeBase(store)
    kb.add_documents([{'title':'待核实资料','content':'EM002 保证收益'}])
    kb.add_documents([{'title':'过期规则','content':'EM002 过期费用','source':'测试来源','source_type':'simulation','version':'old','effective_from':'2020-01-01','effective_to':'2021-01-01','product_id':'EM002'}])
    found=asyncio.run(kb.search_handler({'query':'EM002 费用','top_k':10},None))
    assert all(d['title'] not in ('过期规则','待核实资料') for d in found)

def test_model_auth_failure_is_not_misrepresented_as_missing_user_input():
    from agents.agent_orchestrator import ProductConsultAgent
    class Unauthorized(Exception): status_code=401
    class Messages:
        async def create(self,**kwargs): raise Unauthorized('test')
    agent=ProductConsultAgent(SimpleNamespace(messages=Messages()),'test-model')
    req=Request('EM002 持有10天，赎回10000元手续费怎么算','u1001','c')
    response=asyncio.run(agent.handle(req))
    content,warnings=finalize(req.message,response.content,response.tool_traces)
    assert not response.success and '模型服务认证失败' in content
    assert '请提供产品编号' not in content and warnings==['model_unavailable']

def test_repeated_short_handoff_keeps_same_ticket(api):
    a=send(api,'转人工')
    b=send(api,'转人工',conv_id=a['conv_id'])
    assert a['evidence'][0]['result']['ticket']['ticket_id']==b['evidence'][0]['result']['ticket']['ticket_id']
    assert b['evidence'][0]['result']['deduplicated']


@pytest.mark.parametrize('message',[
    '如果现在全部赎回，手续费怎么算？','现在赎回10000元，手续费多少？',
    'EM001 赎回10000元，持有10天，手续费多少？','怎么投诉？',
    '我不想投诉，只想了解到账时间','不要转人工，告诉我投诉流程',
])
def test_consultations_do_not_create_tickets(api,message):
    r=send(api,message)
    assert not r['escalated'] and 'create_service_ticket' not in r['tools_used']


def test_fee_multiturn_and_product_switch(api):
    first=send(api,'EM002赎回手续费怎么算？')
    result=send(api,'金额10000元，持有10天',conv_id=first['conv_id'])
    assert 'calculate_fee' in result['tools_used'] and '费用 10.00 CNY' in result['response']
    switched=send(api,'换成EM003的手续费呢？',conv_id=first['conv_id'])
    assert 'calculate_fee' not in switched['tools_used'] and '请补充' in switched['response']
    result=send(api,'金额10000元，持有10天',conv_id=first['conv_id'])
    assert 'EM003' in result['response'] and '费用 50.00 CNY' in result['response']


def test_fee_corrections_continue_after_several_partial_turns(api):
    first=send(api,'我要做手续费试算')
    for message in ['产品是EM002','赎回','金额10000元','持有10天','金额改成20000元']:
        result=send(api,message,conv_id=first['conv_id'])
    assert 'calculate_fee' in result['tools_used'] and '费用 20.00 CNY' in result['response']


def test_fee_arguments_bound_to_current_task(store):
    tool=build_finance_tools(store,'product')['calculate_fee']
    req=Request('EM002持有10天，赎回10000元手续费多少？','u1001','c',history=[
        {'role':'user','content':'EM001赎回20000元持有30天手续费多少？'}])
    args=dict(product_id='EM002',action='redeem',amount='10000.00',currency='CNY',holding_days=10)
    for changed in [{'product_id':'EM001'},{'amount':'20000.00'},{'holding_days':30},{'action':'subscribe'}]:
        assert not tool.handler(req,dict(args,**changed))['success']
    assert tool.handler(req,args)['fee']=='10.00'


def test_fee_context_does_not_trust_assistant_or_resume_after_topic_switch():
    from finance.context import resolve_fee_context
    assert not resolve_fee_context('金额10000元，持有10天',[
        {'role':'assistant','content':'EM002赎回手续费怎么算'}]).active
    assert not resolve_fee_context('金额10000元，持有10天',[
        {'role':'user','content':'EM002赎回手续费怎么算'},
        {'role':'user','content':'验证码收不到'}]).active
    assert not resolve_fee_context('EM002和EM003赎回10000元，持有10天，手续费多少').ready


@pytest.mark.parametrize('candidate',[
    '不用担心，本产品保证收益。','不必焦虑，本产品确保本金安全。',
    '不会有问题，已为您赎回。','不能保证收益，但是保证本金。',
])
def test_output_guard_does_not_accept_unrelated_negation(candidate):
    text,warnings=finalize('基金风险',candidate,[])
    assert 'blocked_unsupported_claim' in warnings
    assert '本产品保证收益' not in text and '保证本金' not in text and '已为您赎回' not in text


def test_product_facts_require_evidence_and_cannot_be_rewritten(store):
    fake='EM002申购费率为0.01%，赎回T+0到账。'
    text,warnings=finalize('EM002风险等级是什么',fake,[])
    assert '0.01%' not in text and 'missing_product_evidence' in warnings
    trace={'tool_name':'get_product','result':store.product('EM002')}
    text,_=finalize('EM002风险等级是什么',fake,[trace])
    assert '0.01%' not in text and '0.300%' in text and 'T+3' in text


def test_knowledge_admin_update_survives_restart(store):
    from finance.local_runtime import LocalKnowledgeBase
    kb=LocalKnowledgeBase(store)
    with store.connect() as db:
        original=json.loads(db.execute('SELECT data FROM knowledge LIMIT 1').fetchone()['data'])
    updated=dict(original,version='review-v2',content='管理员更新后的有效资料')
    kb.add_documents([updated])
    LocalKnowledgeBase(store)
    with store.connect() as db:
        actual=json.loads(db.execute('SELECT data FROM knowledge WHERE id=?',(original['doc_id'],)).fetchone()['data'])
    assert actual==updated


def test_call_completion_and_tool_result_metrics_are_separate(store):
    from agents.agent_orchestrator import ProductConsultAgent
    agent=ProductConsultAgent(SimpleNamespace(),'test-model'); agent._demo_mode=True
    agent._business_tools=build_finance_tools(store,'product')
    r=asyncio.run(agent.handle(Request('查询TX2001进度','u1001','c')))
    assert r.success and agent.stats.success==1
    assert r.tool_traces[0]['tool_name']=='get_transaction' and not r.tool_traces[0]['result_success']
    assert agent.stats.tool_calls==1 and agent.stats.tool_results_success==0


def test_model_can_finish_after_third_tool_round(store):
    from agents.agent_orchestrator import ProductConsultAgent
    calls=[]
    steps=[('get_product',{'product_id':'EM002'}),('get_account',{}),
           ('calculate_fee',{'product_id':'EM002','action':'redeem','amount':'10000.00','currency':'CNY','holding_days':10})]
    class Messages:
        async def create(self,**kwargs):
            calls.append(kwargs)
            if steps:
                name,args=steps.pop(0)
                return SimpleNamespace(content=[{'type':'tool_use','name':name,'id':name,'input':args}])
            return SimpleNamespace(content=[{'type':'text','text':'费用试算已取得工具结果。'}])
    agent=ProductConsultAgent(SimpleNamespace(messages=Messages()),'test-model')
    agent._business_tools=build_finance_tools(store,'product')
    r=asyncio.run(agent.handle(Request('EM002赎回10000元持有10天手续费多少','u1001','c')))
    assert r.success and len(r.tool_traces)==3 and len(calls)==4
    assert 'tools' not in calls[-1] and r.tool_traces[-1]['result']['fee']=='10.00'
