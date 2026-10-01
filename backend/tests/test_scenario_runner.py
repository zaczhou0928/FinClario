import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from evaluation.scenario_runner import Dataset, Turn, check_turn, load_dataset, matches, percentile


DATA = Path(__file__).resolve().parents[1]/'evaluation/scenarios/finance_development_v1.json'


def test_dataset_is_development_only_and_contexts_have_sources():
    data=load_dataset(DATA)
    assert len(data.cases)==31 and sum(len(c.turns) for c in data.cases)==45
    assert {c.split for c in data.cases}=={'development'}
    assert all(c.background and c.user_goal for c in data.cases)
    kb=json.loads((DATA.parents[2]/'data/finance/knowledge.json').read_text())
    known={d['doc_id'] for d in kb}
    assert all(set(c.reference_docs)<=known for c in data.cases)


def test_schema_rejects_misspelled_oracles():
    with pytest.raises(ValidationError):
        Turn.model_validate({'message':'测试','expect':{'required_tool':['calculate_fee']}})


def test_score_rejects_correct_sounding_answer_with_wrong_or_missing_tool():
    step=Turn.model_validate({'message':'试算','expect':{
        'required_tools':['calculate_fee'],
        'tool_results':[{'tool':'calculate_fee','result':{'success':True,'fee':'10.00'}}],
        'contains':['10.00 CNY']}})
    body={'response':'费用 10.00 CNY'}
    missing=check_turn(step,200,body,[],0,None)
    assert not all(c['passed'] for c in missing)
    wrong=check_turn(step,200,body,[{'tool_name':'calculate_fee','input':{},'result':{'success':True,'fee':'0.00'}}],0,None)
    assert not all(c['passed'] for c in wrong)
    correct=check_turn(step,200,body,[{'tool_name':'calculate_fee','input':{},'result':{'success':True,'fee':'10.00'}}],0,None)
    assert all(c['passed'] for c in correct)


def test_availability_failure_and_extra_ticket_are_failures():
    step=Turn.model_validate({'message':'咨询','expect':{'ticket_count':0}})
    checks=check_turn(step,200,{'policy_warnings':['model_unavailable']},[],1,None)
    assert {c['name'] for c in checks if not c['passed']}=={'model_available','ticket_count'}


def test_oracles_preserve_types_and_latency_uses_nearest_rank():
    assert not matches({}, {'fee':None})
    assert not matches({'success':1},{'success':True})
    assert matches({'record':{'status':'待到账','extra':1}},{'record':{'status':'待到账'}})
    assert percentile([3,1,2,4],.5)==2 and percentile([3,1,2,4],.95)==4
