# -*- coding: utf-8 -*-
"""行为锁：给整站拍"行为快照"，用于在重构前后逐字对比。

用法:
    # 在某个代码目录（例如 main 分支的 worktree）上录制快照
    python scripts/behavior_lock.py record --root <代码目录> --out base.json
    # 在当前代码上录制快照
    python scripts/behavior_lock.py record --root . --out head.json
    # 对比两份快照，任何差异都会列出并以非零状态退出
    python scripts/behavior_lock.py diff base.json head.json

录制时：
- 使用临时 SQLite 与固定种子数据，时间冻结在 FROZEN_AT，哈希种子固定；
- 分两种环境录制：demo（演示天气、默认功能开关）与 live（"在线"天气缓存、全部功能开关）；
- 禁止一切外部网络，外部依赖统一走兜底分支；
- 以 5 种身份（游客/普通用户/照护人/社区/管理员）访问全部 GET 路由
  与一组固定的 API 写请求，每个请求前把数据库恢复到种子状态；
- 对 HTML/JSON 做规范化（CSRF、静态资源版本号、代码目录等会随环境变化的内容）。
"""
import argparse
import hashlib
import json
import os
import random
import re
import shutil
import socket
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone

FROZEN_AT = '2026-07-20 02:00:00'  # UTC，对应北京时间 10:00，处于高温季
TOKEN = 'behavior-lock-elder-token'
PASSWORD = 'behavior-lock-pass'
ROLES = ('guest', 'user', 'caregiver', 'community', 'admin')

# 路由参数的固定取值（种子数据保证这些对象存在）
PARAM_VALUES = {
    'pair_id': '1',
    'community_id': '1',
    'resource_id': '1',
    'user_id': '2',
    'member_id': '1',
    'reminder_id': '1',
    'community_code': '牛家垄周村',
    'community_name': '牛家垄周村',
    'token': TOKEN,
    'delivery_token': 'behavior-lock-missing-delivery',
}
SKIP_ENDPOINTS = {'static', 'public.amap_proxy', 'public.logout'}

# 带筛选参数的页面变体：覆盖分层、分箱、滞后、日期交换、非法输入等分支
EXTRA_GETS = [
    '/analysis/history?community=牛家垄周村&disease=中暑',
    '/analysis/history?community=岭背徐村&start_date=2026-05-01&end_date=2026-06-30',
    '/analysis/history?community=不存在的村&start_date=2026-07-01',
    '/analysis/heatmap?stratum=elderly&binning=quantile&lag_window=3&min_days=2',
    '/analysis/heatmap?community=牛家垄周村&disease=呼吸系统疾病&stratum=female&lag_window=0',
    '/analysis/heatmap?stratum=bogus&binning=bogus&lag_window=abc&min_days=99&end_date=2026-06-15',
    '/analysis/lag?max_lag=7&stratum=male&community=牛家垄周村',
    '/analysis/lag?max_lag=21&disease=心血管疾病&min_days=1&start_date=2026-04-01&end_date=2026-07-19',
    '/analysis/lag?max_lag=9&stratum=non_elderly',
    '/analysis/community-compare?stratum=elderly&smoothing_alpha=0&top_n=5&min_days=1',
    '/analysis/community-compare?start_date=2026-07-10&end_date=2026-05-01&disease=中暑',
    '/analysis/community-compare?smoothing_alpha=99&top_n=1&stratum=female',
    '/alerts/history?outcome=hit&follow_days=5&min_days=3&threshold_q=0.8',
    '/alerts/history?outcome=false_alarm&location=都昌&alert_type=高温&alert_level=黄色',
    '/alerts/history?outcome=insufficient&threshold_q=0.77&start_date=2026-06-01&end_date=2026-03-01',
    '/alerts/history?threshold_q=abc&follow_days=0&min_days=100&start_date=2026-01-01&end_date=2026-07-20',
    '/alerts/accuracy?threshold_q=0.75&follow_days=7&min_days=3',
    '/alerts/accuracy?threshold_q=0.93&location=都昌&start_date=2026-07-20&end_date=2026-02-01',
    '/alerts/accuracy?alert_type=暴雨&alert_level=红色&min_days=5',
    '/alerts/accuracy?threshold_q=bad&start_date=2026-01-01&end_date=2026-07-20',
    '/analysis/pilot?days=7',
    '/analysis/pilot?days=9999',
]

# 表单 POST（报告导出）
FORM_POSTS = [
    ('/reports/export', {'report_type': 'weekly', 'format': 'excel'}),
    ('/reports/export', {'report_type': 'monthly', 'format': 'pdf'}),
    ('/reports/export', {'report_type': 'monthly', 'format': 'doc'}),
]

# 固定的 API 写请求（JSON 接口，兼容路径与 v1 路径都覆盖）
API_POSTS = [
    ('/api/ml/predict', {'age': 72, 'gender': '女'}),
    ('/api/v1/ml/predict', {'age': 72, 'gender': '女'}),
    ('/api/ml/predict-community', {'community': '牛家垄周村'}),
    ('/api/v1/ml/predict-community', {'community': '牛家垄周村'}),
    ('/api/dlnm/risk', {'temperature': 36, 'age_group': 'elderly'}),
    ('/api/v1/dlnm/risk', {'temperature': 36, 'age_group': 'elderly'}),
    ('/api/chronic/individual', {'age': 72, 'diseases': ['高血压']}),
    ('/api/v1/chronic/individual', {'age': 72, 'diseases': ['高血压']}),
    ('/api/chronic/population', {'community': '牛家垄周村'}),
    ('/api/v1/chronic/population', {'community': '牛家垄周村'}),
    ('/api/ai/ask', {'question': '高温天老人要注意什么？'}),
    ('/api/v1/ai/ask', {'question': '高温天老人要注意什么？'}),
    ('/api/alert/comprehensive', {'community': '牛家垄周村'}),
    ('/api/v1/alert/comprehensive', {'community': '牛家垄周村'}),
]


def _block_network():
    def _refuse(*_args, **_kwargs):
        raise OSError('behavior_lock: network disabled')
    socket.socket.connect = _refuse
    socket.create_connection = _refuse


ALL_FEATURE_FLAGS = (
    'FEATURE_EXPLAIN_OUTPUT', 'FEATURE_EMERGENCY_TRIAGE', 'FEATURE_ELDER_MODE',
    'FEATURE_NOTIFICATIONS', 'FEATURE_HEAT_EXPOSURE_GIS', 'FEATURE_AUDIT_LOGS',
)
# demo: 演示天气 + 默认功能开关；live: 种子里的"在线"天气缓存 + 全部功能开关打开
PROFILES = ('demo', 'live')


def _prepare_env(db_path, profile):
    env = {
        'DATABASE_URI': f'sqlite:///{db_path}',
        'SECRET_KEY': 'behavior-lock-secret-key-0123456789abcdef',
        'PAIR_TOKEN_PEPPER': 'behavior-lock-pepper',
        'DEBUG': 'true',
        'QWEATHER_AUTH_MODE': 'api_key',
        'QWEATHER_KEY': '',
        'QWEATHER_JWT_KID': '',
        'QWEATHER_JWT_PROJECT_ID': '',
        'QWEATHER_JWT_PRIVATE_KEY_PATH': '',
        'AMAP_KEY': '',
        'SILICONFLOW_API_KEY': '',
        'DEMO_MODE': '1',
        'FEATURE_HEAT_EXPOSURE_GIS': '1',
        'RATE_LIMIT_STORAGE_URI': 'memory://',
        'REDIS_URL': '',
        'SENTRY_DSN': '',
    }
    if profile == 'live':
        env['DEMO_MODE'] = '0'
        env.update({flag: '1' for flag in ALL_FEATURE_FLAGS})
    os.environ.update(env)


def _seed(db):
    """写入固定种子数据。只依赖模型字段，不依赖业务服务，确保新旧代码一致。"""
    from core.db_models import (
        Community, CommunityDaily, CoolingResource, DailyStatus, FamilyMember,
        FamilyMemberProfile, HealthDiary, MedicalRecord, MedicationReminder,
        Notification, Pair, PairActionToken, User, WeatherAlert, WeatherData,
    )
    from core.security import hash_pair_token, hash_short_code

    rng = random.Random(20260720)
    now = datetime(2026, 7, 20, 2, 0, 0, tzinfo=timezone.utc)
    today = date(2026, 7, 20)
    names = ['牛家垄周村', '岭背徐村', '徐家湾']
    coords = [(29.333969, 116.199506), (29.337433, 116.198315), (29.338931, 116.19877)]

    for i, name in enumerate(names):
        db.session.add(Community(
            name=name, location=f'都昌县{name}', latitude=coords[i][0], longitude=coords[i][1],
            population=800 + i * 150, elderly_ratio=0.28 + i * 0.03,
            chronic_disease_ratio=0.18 + i * 0.02, vulnerability_index=0.45 + i * 0.1,
            risk_level=['低', '中', '高'][i],
        ))

    users = {}
    for username, role, age in [
        ('bl_admin', 'admin', 45), ('bl_user', 'user', 72),
        ('bl_caregiver', 'caregiver', 40), ('bl_community', 'community', 50),
    ]:
        user = User(
            username=username, role=role, email=f'{username}@example.org', age=age,
            gender='女' if role == 'user' else '男', community=names[0],
            has_chronic_disease=role == 'user',
            chronic_diseases=json.dumps(['高血压'], ensure_ascii=False) if role == 'user' else None,
            created_at=now - timedelta(days=90),
        )
        user.set_password(PASSWORD)
        db.session.add(user)
        users[role] = user
    db.session.flush()

    for location in ['都昌', names[0]]:
        for offset in range(150):
            day = today - timedelta(days=offset)
            base = 30 + 6 * rng.random() if day.month in (6, 7, 8) else 18 + 8 * rng.random()
            tmax = round(base + 4, 1)
            db.session.add(WeatherData(
                date=day, location=location, temperature=round(base, 1),
                temperature_max=tmax, temperature_min=round(base - 6, 1),
                humidity=round(55 + 35 * rng.random(), 1), pressure=1005.0,
                weather_condition='晴' if rng.random() > 0.3 else '多云',
                wind_speed=round(1 + 3 * rng.random(), 1), pm25=round(20 + 40 * rng.random(), 1),
                aqi=int(40 + 60 * rng.random()), is_extreme=tmax >= 37,
                extreme_type='高温' if tmax >= 37 else None,
            ))

    diseases = ['呼吸系统疾病', '心血管疾病', '中暑', '消化系统疾病']
    for i in range(480):
        offset = rng.randint(0, 149)
        db.session.add(MedicalRecord(
            patient_name=f'患者{i:04d}', gender=rng.choice(['男', '女']), age=rng.randint(55, 92),
            visit_time=datetime.combine(today - timedelta(days=offset), datetime.min.time())
            + timedelta(hours=1 + rng.randint(0, 8)),
            department='内科', doctor='医生', disease_category=rng.choice(diseases),
            diagnosis='诊断', chief_complaint='不适', insurance_type='居民医保',
            temperature=36.5, heart_rate=80, blood_pressure='130/85', community=rng.choice(names),
        ))

    for i in range(12):
        db.session.add(WeatherAlert(
            alert_date=now - timedelta(days=i * 9), location='都昌',
            alert_type=['高温', '寒潮', '暴雨'][i % 3], alert_level=['黄色', '橙色', '红色'][i % 3],
            description=f'种子预警{i}', affected_communities=json.dumps(names[: 1 + i % 3], ensure_ascii=False),
            disease_correlation=json.dumps({'呼吸系统疾病': 0.3}, ensure_ascii=False),
        ))

    member = FamilyMember(user_id=users['caregiver'].id, name='周奶奶', relation='母亲', age=78,
                          gender='女', chronic_diseases=json.dumps(['高血压'], ensure_ascii=False),
                          created_at=now - timedelta(days=30))
    db.session.add(member)
    db.session.flush()
    db.session.add(FamilyMemberProfile(member_id=member.id, allergies='无', medications='降压药',
                                       metrics=json.dumps({}), risk_tags=json.dumps(['高龄'], ensure_ascii=False),
                                       created_at=now - timedelta(days=30), updated_at=now - timedelta(days=30)))
    pair = Pair(caregiver_id=users['caregiver'].id, community_code=names[0], member_id=member.id,
                location_query='都昌', elder_code='bl-elder-1', short_code='24681357',
                short_code_hash=hash_short_code('24681357'), status='active',
                created_at=now - timedelta(days=30), last_active_at=now - timedelta(days=1))
    db.session.add(pair)
    db.session.flush()
    db.session.add(PairActionToken(pair_id=pair.id, token_hash=hash_pair_token(TOKEN),
                                   expires_at=now + timedelta(days=7), created_at=now - timedelta(days=1)))
    for offset in range(1, 8):
        db.session.add(DailyStatus(
            pair_id=pair.id, status_date=today - timedelta(days=offset), community_code=names[0],
            risk_level=['低风险', '中风险', '高风险'][offset % 3],
            confirmed_at=now - timedelta(days=offset) if offset % 2 else None,
            help_flag=offset == 3, actions_done_count=offset % 4,
            created_at=now - timedelta(days=offset), updated_at=now - timedelta(days=offset),
        ))
        db.session.add(CommunityDaily(
            community_code=names[0], date=today - timedelta(days=offset), total_people=10,
            confirm_rate=0.6, escalation_rate=0.1, risk_distribution=json.dumps({'高风险': 2}, ensure_ascii=False),
            created_at=now - timedelta(days=offset), updated_at=now - timedelta(days=offset),
        ))
    db.session.add(CoolingResource(community_code=names[0], name='村委会纳凉点', resource_type='纳凉点',
                                   address_hint='村委会一楼', latitude=coords[0][0], longitude=coords[0][1],
                                   open_hours='08:00-20:00', has_ac=True, is_accessible=True,
                                   created_at=now - timedelta(days=60)))
    db.session.add(HealthDiary(user_id=users['user'].id, entry_date=today - timedelta(days=1),
                               symptoms='头晕', severity=2, notes='午后闷热'))
    db.session.add(MedicationReminder(user_id=users['user'].id, medicine_name='降压药', dosage='1片',
                                      frequency='每日一次'))
    db.session.add(Notification(user_id=users['user'].id, title='高温提醒', message='今天注意补水',
                                level='warning', is_read=False, created_at=now - timedelta(hours=3)))
    db.session.commit()


def _seed_live_weather(db):
    """写入标记为 QWeather 来源的新鲜天气缓存，让风险计算走真实分支。"""
    from core.db_models import WeatherCache
    from core.weather import _weather_cache_location

    now = datetime(2026, 7, 20, 2, 0, 0, tzinfo=timezone.utc)
    payload = {
        'temperature': 36.4, 'temperature_max': 38.2, 'temperature_min': 28.6,
        'feels_like': 40.1, 'humidity': 68, 'pressure': 1003, 'weather_condition': '晴',
        'wind_speed': 1.8, 'pm25': 42, 'aqi': 78, 'is_mock': False, 'data_source': 'QWeather',
    }
    keys = set()
    for raw in ['都昌', '都昌县', '牛家垄周村', '岭背徐村', '徐家湾', '九江', '116.20,29.27', None, '']:
        try:
            keys.add(_weather_cache_location(raw))
        except Exception:
            continue
    for key in sorted(k for k in keys if k):
        db.session.add(WeatherCache(location=key, fetched_at=now,
                                    payload=json.dumps(payload, ensure_ascii=False), is_mock=False))
    db.session.commit()


_NORMALIZERS = [
    (re.compile(r'(name="csrf_token"\s+value=")[^"]*'), r'\1<CSRF>'),
    (re.compile(r'(name="csrf-token"\s+content=")[^"]*'), r'\1<CSRF>'),
    (re.compile(r'("csrf_token"\s*:\s*")[^"]*'), r'\1<CSRF>'),
    (re.compile(r'([?&]v=)[0-9a-zA-Z._-]+'), r'\1<V>'),
    (re.compile(r'(nonce=")[^"]*'), r'\1<NONCE>'),
]


def _normalize_text(text, root):
    text = text.replace(root, '<ROOT>')
    for pattern, repl in _NORMALIZERS:
        text = pattern.sub(repl, text)
    return text


def _capture(resp, root):
    ctype = (resp.headers.get('Content-Type') or '').split(';')[0].strip()
    item = {'status': resp.status_code, 'type': ctype}
    location = resp.headers.get('Location')
    if location:
        item['location'] = _normalize_text(location, root)
    data = resp.get_data()
    if ctype == 'application/json':
        try:
            item['json'] = json.loads(_normalize_text(data.decode('utf-8'), root))
            return item
        except ValueError:
            pass
    if ctype.startswith('text/') or ctype in ('application/json', 'application/javascript'):
        item['body'] = _normalize_text(data.decode('utf-8', errors='replace'), root)
    else:
        # 二进制导出（PDF/Excel）内部含生成时间，只记录类型与是否非空
        item['binary'] = bool(data)
    return item


def _fill(rule):
    path = rule.rule
    for arg in rule.arguments:
        if arg not in PARAM_VALUES:
            return None
        path = re.sub(r'<(?:[^:<>]+:)?%s>' % re.escape(arg), PARAM_VALUES[arg], path)
    return path


def record(root, out):
    """每个 profile 在独立子进程中录制（固定哈希种子，隔离进程级缓存），再合并。"""
    import subprocess
    merged = {'frozen_at': FROZEN_AT, 'requests': {}}
    env = dict(os.environ, PYTHONHASHSEED='0')
    for profile in PROFILES:
        part = f'{out}.{profile}.part'
        subprocess.run([sys.executable, os.path.abspath(__file__), '_record_profile',
                        '--root', root, '--out', part, '--profile', profile], env=env, check=True)
        with open(part, encoding='utf-8') as fh:
            for key, value in json.load(fh)['requests'].items():
                merged['requests'][f'[{profile}] {key}'] = value
        os.remove(part)
    with open(out, 'w', encoding='utf-8') as fh:
        json.dump(merged, fh, ensure_ascii=False, indent=1, sort_keys=True)
    print(f'recorded {len(merged["requests"])} responses -> {out}')


def record_profile(root, out, profile):
    root = os.path.abspath(root)
    workdir = tempfile.mkdtemp(prefix='behavior_lock_')
    db_path = os.path.join(workdir, 'live.db')
    seed_path = os.path.join(workdir, 'seed.db')
    _prepare_env(db_path, profile)
    os.chdir(root)
    sys.path.insert(0, root)
    _block_network()
    random.seed(0)

    import logging
    logging.disable(logging.CRITICAL)
    import warnings
    warnings.simplefilter('ignore')

    # 先导入应用（含 pandas 等 C 扩展）再冻结时间，freezegun 会替换已加载模块里的 datetime
    import pandas  # noqa: F401
    from core.app import create_app
    from core.extensions import db, limiter
    from freezegun import freeze_time
    freezer = freeze_time(FROZEN_AT, ignore=['pandas', 'numpy', 'scipy', 'sklearn'])
    freezer.start()

    app = create_app()
    app.config['TESTING'] = True
    limiter.enabled = False

    with app.app_context():
        db.create_all()
        _seed(db)
        if profile == 'live':
            _seed_live_weather(db)
        db.session.remove()
        db.engine.dispose()
    shutil.copyfile(db_path, seed_path)

    def reset_db():
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
        shutil.copyfile(seed_path, db_path)

    requests_plan = []
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: (r.rule, r.endpoint)):
        if rule.endpoint in SKIP_ENDPOINTS or 'GET' not in rule.methods:
            continue
        path = _fill(rule)
        if path is not None:
            requests_plan.append(('GET', path, None))
    requests_plan.extend(('GET', path, None) for path in EXTRA_GETS)
    requests_plan.extend(('POST', path, payload) for path, payload in API_POSTS)
    requests_plan.extend(('FORM', path, payload) for path, payload in FORM_POSTS)

    snapshot = {'frozen_at': FROZEN_AT, 'requests': {}}
    for role in ROLES:
        client = app.test_client()
        with client.session_transaction() as sess:
            sess['_csrf_token'] = 'behavior-lock-csrf'
        if role != 'guest':
            reset_db()
            client.post('/login', data={'username': f'bl_{role}', 'password': PASSWORD,
                                        'csrf_token': 'behavior-lock-csrf'})
        for method, path, payload in requests_plan:
            reset_db()
            random.seed(0)
            if method == 'GET':
                resp = client.get(path)
            elif method == 'FORM':
                form = dict(payload, csrf_token='behavior-lock-csrf')
                resp = client.post(path, data=form)
                payload_key = '&'.join(f'{k}={v}' for k, v in sorted(payload.items()))
                path = f'{path}?{payload_key}'
            else:
                resp = client.post(path, json=payload, headers={'X-CSRF-Token': 'behavior-lock-csrf'})
            snapshot['requests'][f'{role} {method} {path}'] = _capture(resp, root)

    freezer.stop()
    with open(out, 'w', encoding='utf-8') as fh:
        json.dump(snapshot, fh, ensure_ascii=False, indent=1, sort_keys=True)
    shutil.rmtree(workdir, ignore_errors=True)


def _digest(item):
    return hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def diff(base_path, head_path, show=3):
    import difflib
    with open(base_path, encoding='utf-8') as fh:
        base = json.load(fh)['requests']
    with open(head_path, encoding='utf-8') as fh:
        head = json.load(fh)['requests']
    missing = sorted(set(base) - set(head))
    added = sorted(set(head) - set(base))
    changed = sorted(k for k in set(base) & set(head) if _digest(base[k]) != _digest(head[k]))
    for key in missing:
        print(f'[缺失] {key}')
    for key in added:
        print(f'[新增] {key}')
    for key in changed:
        print(f'[变化] {key}')
    def as_lines(item):
        meta = {k: v for k, v in item.items() if k != 'body'}
        lines = json.dumps(meta, ensure_ascii=False, indent=1, sort_keys=True).splitlines()
        return lines + item.get('body', '').splitlines()

    for key in changed[:show]:
        print(f'--- {key}')
        print('\n'.join(list(difflib.unified_diff(
            as_lines(base[key]), as_lines(head[key]), 'base', 'head', lineterm='', n=2
        ))[:80]))
    print(f'共 {len(base)} 个基线响应：缺失 {len(missing)}，变化 {len(changed)}，新增 {len(added)}')
    return 1 if (missing or changed) else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='cmd', required=True)
    rec = sub.add_parser('record')
    rec.add_argument('--root', default='.')
    rec.add_argument('--out', required=True)
    part = sub.add_parser('_record_profile')
    part.add_argument('--root', default='.')
    part.add_argument('--out', required=True)
    part.add_argument('--profile', choices=PROFILES, required=True)
    dif = sub.add_parser('diff')
    dif.add_argument('base')
    dif.add_argument('head')
    dif.add_argument('--show', type=int, default=3)
    args = parser.parse_args()
    if args.cmd == 'record':
        out = os.path.abspath(args.out)
        record(args.root, out)
        return 0
    if args.cmd == '_record_profile':
        record_profile(args.root, args.out, args.profile)
        return 0
    return diff(args.base, args.head, args.show)


if __name__ == '__main__':
    sys.exit(main())
