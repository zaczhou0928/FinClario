"""工单标识不得被私人号码脱敏破坏。"""
import json
import pytest
from fastapi.testclient import TestClient
from finance.store import sanitize


def test_sanitize_preserves_ticket_and_redacts_private_numbers():
    text = '工单 SR123456789012，电话13812345678，卡号6222021234567890123，密码abcd1234'
    result = sanitize(text)
    assert 'SR123456789012' in result
    assert '13812345678' not in result and '6222021234567890123' not in result
    assert 'abcd1234' not in result


@pytest.mark.parametrize('ticket_id', ['SR123456789012', 'SRA12345678901'])
def test_numeric_ticket_can_be_queried_and_updated(tmp_path, monkeypatch, ticket_id):
    for key, value in {'ECHOMIND_MODE':'demo','ECHOMIND_STORAGE':'local','PROMETHEUS_PORT':'0',
                       'ECHOMIND_DB_PATH':str(tmp_path/'finance.sqlite3')}.items():
        monkeypatch.setenv(key, value)
    import api.main as service
    with TestClient(service.app) as client:
        result=service._finance_store.create_ticket('u1001','numeric-ticket','投诉支付异常','核实扣款','PAY1001')
        ticket=result['ticket']; old=ticket['ticket_id']; ticket['ticket_id']=ticket_id
        with service._finance_store.connect() as db:
            db.execute('UPDATE tickets SET id=?,data=? WHERE id=?', (ticket_id,json.dumps(ticket),old))
        headers={'Authorization':'Bearer demo-u1001'}
        query=client.post('/chat',headers=headers,json={'message':'查询工单 '+ticket_id})
        assert query.status_code==200
        assert query.json()['evidence'][0]['result']['ticket']['ticket_id']==ticket_id
        update=client.post('/chat',headers=headers,json={'message':f'补充工单 {ticket_id}，发生时间2026-10-02，希望核实扣款原因'})
        assert update.status_code==200
        assert update.json()['evidence'][0]['result']['ticket']['status']=='待人工核验'
