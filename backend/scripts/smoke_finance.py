#!/usr/bin/env python3
"""对运行中的服务做最小 HTTP 冒烟。会创建一个模拟工单；无资金操作。"""
import argparse
import uuid
import httpx

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--base-url',default='http://127.0.0.1:8000')
    args=parser.parse_args()
    with httpx.Client(base_url=args.base_url,headers={'Authorization':'Bearer demo-u1001'},timeout=90) as c:
        c.get('/health').raise_for_status()
        def chat(message,**kw):
            r=c.post('/chat',json={'message':message,**kw});r.raise_for_status();return r.json()
        p=chat('EM002 的风险和开放安排是什么？')
        assert 'get_product' in p['tools_used']
        fee=chat('EM002 持有10天，全部赎回10000元手续费怎么算？')
        assert not fee['escalated'] and '10.00 CNY' in fee['response']
        assert '无权访问' in chat('查询 TX2001 进度')['response']
        tx=chat('查询 TX1001 赎回进度');assert 'get_transaction' in tx['tools_used']
        mixed=chat('登录报401，同时 PAY1001 被重复扣款')
        assert set(mixed['agent_types'])=={'technical','billing'}
        message='我要投诉 PAY1001，发生时间2026-10-02，希望核实扣款原因'
        request_id='smoke-'+uuid.uuid4().hex
        ticket=chat(message,request_id=request_id)
        assert chat(message,request_id=request_id)==ticket
        ticket_id=ticket['evidence'][0]['result']['ticket']['ticket_id']
        assert ticket_id in chat('查询工单 '+ticket_id)['response']
        trace=c.get('/trace/tool/'+fee['request_id']).json()
        assert trace['found'] and trace['trace']['tool_calls']
        print('HTTP 冒烟通过：产品、费用、归属隔离、申赎查询、多角色协作、工单创建/幂等/查询、工具轨迹')
        print('实际模拟工单编号：'+ticket_id)
        print('运行模式：'+fee['mode']+'；此检查不测量模型准确率。')
if __name__=='__main__': main()
