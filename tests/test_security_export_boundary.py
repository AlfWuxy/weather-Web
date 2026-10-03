"""生产聚合导出和非正式态异常日志也必须保持安全边界。"""
import csv
import io
import logging

import pytest


@pytest.mark.parametrize('formula', ['=1+1', '+SUM(1,1)', '-1+1', '@SUM(1,1)',
                                   '\t=1+1', '\r=1+1', '  =1+1', '\ufeff=1+1'])
def test_aggregate_export_neutralizes_historical_formula(app, db_session, admin_client, formula):
    from core.db_models import UsageEvent
    from core.time_utils import utcnow
    db_session.add(UsageEvent(event_type=formula, source=formula, created_at=utcnow()))
    db_session.commit()
    response = admin_client.get('/analysis/pilot/export.csv')
    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.data.decode('utf-8-sig'))))
    assert rows[1][1:3] == ["'" + formula, "'" + formula]
    assert rows[1][3] == '1'


def test_transaction_exception_does_not_log_bound_values(app, db_session, monkeypatch, caplog):
    from sqlalchemy.exc import IntegrityError
    from utils.database import atomic_transaction
    address = '测试县虚构路987号8栋203室'
    def fail(*args, **kwargs):
        raise IntegrityError('INSERT INTO pairs VALUES (?)', (address,), RuntimeError('synthetic failure'))
    monkeypatch.setattr(db_session, 'commit', fail)
    with caplog.at_level(logging.DEBUG), pytest.raises(IntegrityError), atomic_transaction(db_session):
        pass
    assert address not in caplog.text


def test_usage_write_failure_does_not_log_bound_values(app, db_session, monkeypatch, caplog):
    from core.extensions import db
    from core.usage import log_usage_event
    address = '测试县虚构路987号8栋203室'
    def fail():
        raise RuntimeError('SQL parameters: ' + address)
    with app.app_context(), caplog.at_level(logging.DEBUG):
        monkeypatch.setattr(db.session, 'commit', fail)
        assert log_usage_event('template_copy') is None
    assert address not in caplog.text
