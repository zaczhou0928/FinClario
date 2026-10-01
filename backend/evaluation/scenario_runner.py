"""金融场景回放：真实 API/工具/数据库，规则断言与模型质量评分分开。"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import os
import platform
import tempfile
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[1]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ToolOracle(StrictModel):
    tool: str
    result: dict = Field(default_factory=dict)
    input: dict = Field(default_factory=dict)


class Expectation(StrictModel):
    http_status: int = 200
    required_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    any_tools: list[str] = Field(default_factory=list)
    required_doc_ids: list[str] = Field(default_factory=list)
    contains: list[str] = Field(default_factory=list)
    not_contains: list[str] = Field(default_factory=list)
    fields: dict = Field(default_factory=dict)
    tool_results: list[ToolOracle] = Field(default_factory=list)
    ticket_count: int | None = None
    same_ticket: bool = False


class RecordChange(StrictModel):
    kind: Literal['accounts','transactions','payments']
    id: str
    changes: dict


class Turn(StrictModel):
    message: str = ''
    user: Literal['u1001','u1002'] | None = None
    new_conversation: bool = False
    replay_previous: bool = False
    before: list[RecordChange] = Field(default_factory=list)
    expect: Expectation


class Scenario(StrictModel):
    id: str
    family: str
    split: Literal['development','holdout'] = 'development'
    background: str
    user_goal: str
    user: Literal['u1001','u1002'] = 'u1001'
    reference_docs: list[str]
    initial_records: list[RecordChange] = Field(default_factory=list)
    turns: list[Turn] = Field(min_length=1)


class Dataset(StrictModel):
    version: str
    provenance: str
    reference_date: str
    cases: list[Scenario] = Field(min_length=1)


def load_dataset(path):
    data=Dataset.model_validate_json(Path(path).read_text())
    date.fromisoformat(data.reference_date)
    ids=[c.id for c in data.cases]
    if len(ids)!=len(set(ids)): raise ValueError('场景 ID 重复')
    families={}
    for case in data.cases:
        if case.family in families and families[case.family]!=case.split:
            raise ValueError('同一场景族不能跨开发集与留出集')
        families[case.family]=case.split
        for step in case.turns:
            if not step.message and not step.replay_previous: raise ValueError('非重放轮次必须有消息')
    return data


def matches(actual, expected):
    """独立黄金字段的部分匹配；缺失字段不能等同于 null。"""
    if isinstance(expected,dict):
        return isinstance(actual,dict) and all(k in actual and matches(actual[k],v) for k,v in expected.items())
    return type(actual) is type(expected) and actual==expected


def check_turn(step, status, response, traces, ticket_count, previous_ticket):
    checks=[]
    def add(name,ok): checks.append({'name':name,'passed':bool(ok)})
    expect=step.expect
    add('http_status',status==expect.http_status)
    text=response.get('response','')
    names=[t['tool_name'] for t in traces]
    for tool in expect.required_tools: add('required_tool:'+tool,tool in names)
    for tool in expect.forbidden_tools: add('forbidden_tool:'+tool,tool not in names)
    if expect.any_tools: add('any_tools',any(t in names for t in expect.any_tools))
    doc_ids={d.get('doc_id') for t in traces for d in t.get('result',{}).get('results',[])}
    for doc_id in expect.required_doc_ids: add('retrieved_doc:'+doc_id,doc_id in doc_ids)
    for value in expect.contains: add('contains:'+value,value in text)
    for value in expect.not_contains: add('not_contains:'+value,value not in text)
    for key,value in expect.fields.items(): add('field:'+key,key in response and matches(response[key],value))
    for i,oracle in enumerate(expect.tool_results):
        add('tool_oracle:'+str(i)+':'+oracle.tool,any(t['tool_name']==oracle.tool and matches(t.get('result',{}),oracle.result) and matches(t.get('input',{}),oracle.input) for t in traces))
    if expect.ticket_count is not None: add('ticket_count',ticket_count==expect.ticket_count)
    if expect.same_ticket:
        ids=[t.get('result',{}).get('ticket',{}).get('ticket_id') for t in traces]
        add('same_ticket',bool(previous_ticket) and previous_ticket in ids)
    add('model_available','model_unavailable' not in response.get('policy_warnings',[]))
    return checks


def apply_changes(store, changes):
    for change in changes:
        with store.connect() as db:
            row=db.execute('SELECT data FROM records WHERE kind=? AND id=?',(change.kind,change.id)).fetchone()
            if not row: raise ValueError('场景预置记录不存在：'+change.id)
            record=json.loads(row['data'])
            if 'user_id' in change.changes: raise ValueError('状态补丁不能更改归属；跨用户场景请切换演示身份')
            record.update(change.changes)
            db.execute('UPDATE records SET data=? WHERE kind=? AND id=?',(json.dumps(record,ensure_ascii=False),change.kind,change.id))


def record_snapshot(store):
    with store.connect() as db:
        return [tuple(r) for r in db.execute('SELECT kind,id,user_id,data FROM records ORDER BY kind,id')]


def run_case(case, reference_date, mode):
    # 一个场景一个新数据库和应用生命周期，不读取/污染演示数据库。
    from fastapi.testclient import TestClient
    from api import main
    rows=[]; conv=None; previous_payload=None; previous_ticket=None
    with tempfile.TemporaryDirectory(prefix='echomind-eval-') as directory:
        os.environ['ECHOMIND_DB_PATH']=str(Path(directory)/'finance.sqlite3')
        main._conversation_locks.clear()
        with contextlib.redirect_stdout(io.StringIO()), TestClient(main.app,raise_server_exceptions=False) as client:
            store=main._finance_store
            store.as_of=reference_date
            generation_settings={role.value:[{'model':a._model,'temperature':a.profile.temperature,'max_tokens':a.profile.max_tokens} for a in agents]
                                 for role,agents in main._orchestrator._pool.items()}
            apply_changes(store,case.initial_records)
            for index,step in enumerate(case.turns):
                user=step.user or case.user
                apply_changes(store,step.before)
                before=record_snapshot(store)
                if step.replay_previous:
                    if previous_payload is None: raise ValueError('首轮不能重放')
                    payload=dict(previous_payload)
                else:
                    payload={'message':step.message.replace('{ticket_id}',previous_ticket or 'SRMISSING000'),
                             'conv_id':None if step.new_conversation else conv,'request_id':str(uuid.uuid4())}
                headers={'Authorization':'Bearer demo-'+user}
                started=time.perf_counter()
                reply=client.post('/chat',headers=headers,json=payload)
                elapsed=(time.perf_counter()-started)*1000
                try: body=reply.json()
                except ValueError: body={'response':'非 JSON 错误响应'}
                traces=[]
                if body.get('request_id'):
                    trace=client.get('/trace/tool/'+body['request_id'],headers=headers).json()
                    traces=trace.get('trace',{}).get('tool_calls',[])
                with store.connect() as db:
                    count=db.execute('SELECT COUNT(*) FROM tickets').fetchone()[0]
                checks=check_turn(step,reply.status_code,body,traces,count,previous_ticket)
                if reply.status_code==200:
                    checks.append({'name':'runtime_mode','passed':body.get('mode')==mode})
                checks.append({'name':'business_records_unchanged','passed':before==record_snapshot(store)})
                for trace in traces:
                    ticket=trace.get('result',{}).get('ticket')
                    if ticket: previous_ticket=ticket['ticket_id']
                rows.append({'turn':index+1,'user':user,'message':payload['message'],'replay':step.replay_previous,
                             'http_status':reply.status_code,'response':body,'traces':traces,'checks':checks,
                             'passed':all(c['passed'] for c in checks),'client_elapsed_ms':round(elapsed,3),
                             'input_tokens':None,'output_tokens':None,'cost':None,
                             'human_quality_scores':None})
                if body.get('conv_id'): conv=body['conv_id']
                previous_payload=payload
    return {'id':case.id,'family':case.family,'split':case.split,'background':case.background,
            'generation_settings':generation_settings,'passed':all(r['passed'] for r in rows),'turns':rows}


def source_fingerprint():
    # 白名单目录的源文件摘要；不读取 .env、凭证或运行数据库。
    digest=hashlib.sha256()
    for directory in ('agents','api','core','finance','memory','mcp','monitor','skills','evaluation','data/finance'):
        for path in sorted((ROOT/directory).rglob('*')):
            if path.is_file() and path.suffix in ('.py','.json','.md') and '__pycache__' not in path.parts and 'runs' not in path.parts:
                digest.update(str(path.relative_to(ROOT)).encode()); digest.update(path.read_bytes())
    return digest.hexdigest()


def percentile(values, quantile):
    return round(sorted(values)[max(0,math.ceil(len(values)*quantile)-1)],3) if values else None


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset',type=Path,default=ROOT/'evaluation/scenarios/finance_development_v1.json')
    parser.add_argument('--mode',choices=('demo','llm'),required=True)
    parser.add_argument('--repeat',type=int,default=1)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.repeat<1: parser.error('--repeat 必须大于零')
    data=load_dataset(args.dataset)
    os.environ.update(ECHOMIND_MODE=args.mode,ECHOMIND_STORAGE='local',PROMETHEUS_PORT='0',LOG_LEVEL='ERROR',ECHOMIND_ENABLE_WEBHOOK='0')
    results=[]
    for repetition in range(args.repeat):
        for case in data.cases:
            result=run_case(case,data.reference_date,args.mode)
            result['repetition']=repetition+1
            results.append(result)
    turns=[t for r in results for t in r['turns']]
    elapsed=[t['client_elapsed_ms'] for t in turns if not t['replay']]
    checked=[c for t in turns for c in t['checks']]
    report={'run_id':str(uuid.uuid4()),'created_at':datetime.now(timezone.utc).isoformat(),
            'mode':args.mode,'model':os.getenv('ANTHROPIC_MODEL') if args.mode=='llm' else None,
            'storage':'local','transport':'in_process_asgi_testclient','reference_date':data.reference_date,
            'dataset_version':data.version,'dataset_sha256':hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
            'source_sha256':source_fingerprint(),'python':platform.python_version(),'platform':platform.platform(),
            'repeat':args.repeat,'unique_scenarios':len(data.cases),'scenario_runs':len(results),
            'passed_scenario_runs':sum(r['passed'] for r in results),'turns':len(turns),
            'passed_turns':sum(t['passed'] for t in turns),'assertions':len(checked),
            'passed_assertions':sum(c['passed'] for c in checked),
            'model_unavailable_turns':sum('model_unavailable' in t['response'].get('policy_warnings',[]) for t in turns),
            'latency_ms':{'p50':percentile(elapsed,.5),'p95':percentile(elapsed,.95),'n':len(elapsed),
                          'scope':'单进程 ASGI 请求耗时，含意图识别及会话读写；不含应用启动、真实网络与重放缓存命中'},
            'input_tokens':None,'output_tokens':None,'cost':None,'human_quality_scores':None,
            'resume_claim_ready':False,
            'limitations':['合成开发场景，非真实客户样本、非独立盲测','规则断言不等于回答质量或合规认证',
                           'Token/成本/人工评分尚未采集；未进行负载测试'],
            'results':results}
    report['by_family']={family:{'scenario_runs':sum(r['family']==family for r in results),
                               'passed':sum(r['family']==family and r['passed'] for r in results)}
                         for family in sorted({r['family'] for r in results})}
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    lines=[f"# FinClario 场景回放：{args.mode}",f"\n数据版本：{data.version}；场景运行 {len(results)}；通过 {report['passed_scenario_runs']}。",
           f"\n轮次 {len(turns)}；断言 {len(checked)}；通过断言 {report['passed_assertions']}。",
           '\n这是合成开发集回归结果，不能表述为模型准确率。',
           '\n完整响应、工具轨迹、失败断言、环境及摘要见 [report.json](report.json)。',
           '\n| 场景 | 重复轮次 | 结果 | 失败断言 |','| --- | --- | --- | --- |']
    for r in results:
        failed=[str(t['turn'])+':'+c['name'] for t in r['turns'] for c in t['checks'] if not c['passed']]
        lines.append(f"| {r['id']} | {r['repetition']} | {'通过' if r['passed'] else '失败'} | {'；'.join(failed)} |")
    (args.output/'summary.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({k:report[k] for k in ('mode','unique_scenarios','scenario_runs','passed_scenario_runs','turns','assertions','passed_assertions')},ensure_ascii=False))
    return 0 if all(r['passed'] for r in results) else 1


if __name__=='__main__':
    raise SystemExit(main())
