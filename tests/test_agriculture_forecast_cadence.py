"""公开模型响应的离线回归；固定回放时钟，非农户观察，n_real=0。"""
from pathlib import Path
import subprocess
import sys


def test_documented_ifs_long_run_and_missing_step_rejection():
    root = Path(__file__).resolve().parents[1]
    program = r"""
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
sys.path.insert(0, 'vendor/yilao_agriculture')
from yilao_agri.forecast_run import make_forecast_run_snapshot, derive_run_weather, validate_run_snapshot
fixture=json.loads(Path('tests/fixtures/agriculture_ifs025_12z_public.json').read_text())
assert fixture['n_real']==0 and fixture['field_validated'] is False
_,meta,response=fixture['responses'];now=datetime.fromisoformat(fixture['clock_start'])
kwargs=dict(request_url=response['request_url'],metadata_url=meta['request_url'],
 metadata_queried_at=now.isoformat(),metadata_retrieved_at=(now+timedelta(seconds=1)).isoformat(),
 queried_at=(now+timedelta(seconds=2)).isoformat(),retrieved_at=(now+timedelta(seconds=3)).isoformat())
raw=response['raw_response_text'].encode();metadata=meta['raw_response_text'].encode()
weather=derive_run_weather(make_forecast_run_snapshot(raw,metadata,**kwargs))
assert len(weather['records'])==166 and weather['issued_at'] is None
assert validate_run_snapshot(weather,now+timedelta(seconds=4)).hour==12
for index in (1,47,50):
 bad=json.loads(metadata);bad['valid_times'].pop(index)
 try:make_forecast_run_snapshot(raw,json.dumps(bad).encode(),**kwargs)
 except ValueError:pass
 else:raise AssertionError('缺口不能被误认为可接受的变步长')
"""
    result = subprocess.run([sys.executable, '-B', '-c', program], cwd=root,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
