# -*- coding: utf-8 -*-
"""行为锁的连续业务场景（场景锁）。

每个场景从种子数据出发，按顺序执行步骤；场景内部保留数据库和各身份的会话，
场景之间恢复种子。每一步之后记录响应以及这一步造成的数据库变化。

步骤格式:
    ('as', 身份)                     切换身份。身份为 guest/user/caregiver/community/admin/caregiver2，
                                     加 '#n' 后缀表示同一身份的另一个独立浏览器（例如 'guest#2'）
    ('GET', 路径)                    路径可用 {变量} 引用上下文
    ('POST', 路径, 表单)             表单值可用 {变量}；列表值表示多选
    ('let', 变量名, 函数)            函数接收 (db, models)，返回值写入上下文，例如取刚创建对象的 id
    ('set', 说明, 函数)              函数接收 (db, models)，直接修改数据库（制造过期、撤销等状态）
"""
from datetime import timedelta

ELDER_CODE = '24681357'           # 种子配对的短码
LINK_CODE = '13572468'            # 种子里尚未赎回的绑定短码
LINK_TOKEN = 'behavior-lock-link-token'
DEBRIEF = {'question_1': '有照做', 'question_2': '还好', 'question_3': '需要纳凉点',
           'difficulty': '午后太热'}


def _max_id(model):
    def pick(db, models):
        value = db.session.query(db.func.max(getattr(models, model).id)).scalar()
        return str(value)
    return pick


def _expire_elder_access(db, models):
    """撤销种子配对的行动令牌，并让短码过期。"""
    from core.time_utils import utcnow
    now = utcnow()
    for token in models.PairActionToken.query.all():
        token.revoked_at = now
    pair = db.session.get(models.Pair, 1)
    pair.short_code_expires_at = now - timedelta(days=1)


def _expire_link(db, models):
    from core.time_utils import utcnow
    link = models.PairLink.query.filter_by(short_code=LINK_CODE).first()
    link.expires_at = utcnow() - timedelta(minutes=1)


def _prepare_extreme_day(db, models):
    """清空最近一天的预警，并把今天的天气标记为极端高温，触发仪表盘自动生成预警。"""
    from core.time_utils import today_local, utcnow
    models.WeatherAlert.query.filter(models.WeatherAlert.alert_date >= utcnow() - timedelta(days=1)).delete()
    for row in models.WeatherData.query.filter_by(date=today_local()).all():
        row.is_extreme = True
        row.extreme_type = '高温'
        row.temperature_max = 39.5


def _prepare_community_mix(db, models):
    """构造当天各种状态的配对：停用配对、跨社区记录、超时未确认、已确认求助、各接力阶段，另留一个空社区。"""
    from core.time_utils import today_local, utcnow
    now = utcnow()
    today = today_local()
    rows = [
        # (配对所属社区, 配对状态, 记录所属社区, 创建距今, 已确认, 求助, 接力阶段, 风险)
        ('牛家垄周村', 'inactive', '牛家垄周村', timedelta(hours=5), False, True, 'community', '高风险'),
        ('牛家垄周村', 'active', '牛家垄周村', timedelta(hours=3), False, False, 'none', '高风险'),
        ('牛家垄周村', 'active', '牛家垄周村', timedelta(hours=3), False, False, 'caregiver', '极高'),
        ('牛家垄周村', 'active', '牛家垄周村', timedelta(hours=4), False, False, 'emergency', '中风险'),
        ('牛家垄周村', 'active', '牛家垄周村', timedelta(hours=1), False, False, 'none', '低风险'),
        ('牛家垄周村', 'active', '牛家垄周村', timedelta(hours=6), True, True, 'backup', '高风险'),
        ('岭背徐村', 'active', '牛家垄周村', timedelta(hours=3), False, True, 'none', '中风险'),
        ('岭背徐村', 'active', '岭背徐村', timedelta(minutes=30), True, False, 'none', '未知'),
    ]
    for index, (pair_comm, pair_status, status_comm, age, confirmed, help_flag, stage, risk) in enumerate(rows):
        code = f'5{index:07d}'
        pair = models.Pair(caregiver_id=3, community_code=pair_comm, elder_code=f'bl-mix-{index}', short_code=code,
                           status=pair_status, created_at=now - timedelta(days=2))
        db.session.add(pair)
        db.session.flush()
        db.session.add(models.DailyStatus(
            pair_id=pair.id, status_date=today, community_code=status_comm, risk_level=risk,
            confirmed_at=now - timedelta(minutes=10) if confirmed else None, help_flag=help_flag,
            actions_done_count=index % 3, relay_stage=stage, created_at=now - age, updated_at=now - age,
        ))


SCENARIOS = [
    {
        'name': 'S1-老人令牌链接正常流程',
        'as': 'guest',
        'steps': [
            ('GET', '/e/{TOKEN}'),
            ('POST', '/e/{TOKEN}/checkin', {'short_code': ELDER_CODE, 'actions_done': ['hydrate', 'cool']}),
            ('POST', '/e/{TOKEN}/help', {'short_code': ELDER_CODE}),
            ('POST', '/e/{TOKEN}/debrief', dict(DEBRIEF, short_code=ELDER_CODE, debrief_optin='1')),
            ('GET', '/e/{TOKEN}/debrief'),
            ('as', 'caregiver'),
            ('GET', '/caregiver'),
            ('GET', '/caregiver/pair/1'),
            ('as', 'community'),
            ('GET', '/community'),
            ('GET', '/community/牛家垄周村'),
        ],
    },
    {
        'name': 'S2-短码入口与重复提交',
        'as': 'guest',
        'steps': [
            ('GET', '/action?short_code=' + ELDER_CODE),
            ('POST', '/action', {'short_code': ELDER_CODE}),
            ('POST', '/action/confirm', {'short_code': ELDER_CODE, 'actions_done': ['hydrate']}),
            ('POST', '/action/confirm', {'short_code': ELDER_CODE, 'actions_done': ['hydrate', 'cool']}),
            ('POST', '/action/help', {'short_code': ELDER_CODE}),
            ('POST', '/action/help', {'short_code': ELDER_CODE}),
            ('POST', '/action/debrief', dict(DEBRIEF, short_code=ELDER_CODE, debrief_optin='0')),
            ('POST', '/action/debrief', dict(DEBRIEF, short_code=ELDER_CODE, debrief_optin='0', difficulty='匿名第二次')),
            ('POST', '/action/debrief', dict(DEBRIEF, short_code=ELDER_CODE, debrief_optin='1')),
            ('POST', '/action/debrief', dict(DEBRIEF, short_code=ELDER_CODE, debrief_optin='1', difficulty='更新')),
            ('POST', '/elder/enter', {'short_code': ELDER_CODE}),
            ('as', 'community'),
            ('GET', '/community/牛家垄周村'),
        ],
    },
    {
        'name': 'S3-失效与错误输入',
        'as': 'guest',
        'steps': [
            ('POST', '/action', {'short_code': ''}),
            ('POST', '/action/confirm', {'short_code': '99999999'}),
            ('POST', '/e/behavior-lock-bad-token/checkin', {'short_code': ELDER_CODE}),
            ('POST', '/e/{TOKEN}/help', {'short_code': '99999999'}),
            ('POST', '/action', {'short_code': '00000001'}),
            ('POST', '/action', {'short_code': '00000002'}),
            ('POST', '/action', {'short_code': '00000003'}),
            ('POST', '/action', {'short_code': '00000004'}),
            ('POST', '/action', {'short_code': '00000005'}),
            ('POST', '/action', {'short_code': '00000006'}),
            ('POST', '/action', {'short_code': ELDER_CODE}),
            ('as', 'guest#2'),
            ('set', '撤销行动令牌并让短码过期', _expire_elder_access),
            ('GET', '/e/{TOKEN}'),
            ('POST', '/e/{TOKEN}/checkin', {'short_code': ELDER_CODE}),
            ('POST', '/action', {'short_code': ELDER_CODE}),
        ],
    },
    {
        'name': 'S4-绑定短码赎回',
        'as': 'guest',
        'steps': [
            ('POST', '/action', {'short_code': LINK_CODE}),
            ('POST', '/action', {'short_code': LINK_CODE, 'token': 'wrong-token'}),
            ('POST', '/action', {'short_code': LINK_CODE, 'token': LINK_TOKEN}),
            ('POST', '/action/confirm', {'short_code': LINK_CODE, 'actions_done': ['hydrate']}),
            ('as', 'guest#2'),
            ('POST', '/action', {'short_code': LINK_CODE, 'token': LINK_TOKEN}),
            ('as', 'guest#3'),
            ('set', '让绑定短码过期', _expire_link),
            ('POST', '/action', {'short_code': LINK_CODE, 'token': LINK_TOKEN}),
            ('as', 'caregiver'),
            ('GET', '/pairs'),
        ],
    },
    {
        'name': 'S5-照护人操作',
        'as': 'caregiver',
        'steps': [
            ('GET', '/pairs'),
            ('POST', '/caregiver/pair/create', {'location_query': '都昌', 'member_id': '1'}),
            ('let', 'new_pair', _max_id('Pair')),
            ('GET', '/caregiver/pair/{new_pair}'),
            ('POST', '/pairs', {'location_query': '岭背徐村'}),
            ('POST', '/pairs', {'community_code': '徐家湾'}),
            ('POST', '/caregiver/pair/1/action-log',
             {'caregiver_actions': ['remind', 'neighbor', 'unknown'], 'caregiver_note': '已电话提醒'}),
            ('POST', '/caregiver/relay/backup', {'pair_id': '1'}),
            ('POST', '/caregiver/relay/escalate', {'pair_id': '1'}),
            ('POST', '/pairs/1/escalate', {}),
            ('POST', '/pairs/1/backup', {}),
            ('POST', '/caregiver/relay/escalate', {'pair_id': '999'}),
            ('GET', '/caregiver/wechat_template?short_code=' + ELDER_CODE + '&token={TOKEN}&community_code=牛家垄周村'),
            ('GET', '/caregiver/pair/1'),
            ('as', 'community'),
            ('GET', '/community/牛家垄周村'),
        ],
    },
    {
        'name': 'S6-越权与未登录',
        'as': 'caregiver2',
        'steps': [
            ('GET', '/caregiver/pair/1'),
            ('POST', '/caregiver/pair/1/action-log', {'caregiver_actions': ['remind'], 'caregiver_note': '越权'}),
            ('POST', '/pairs/1/escalate', {}),
            ('POST', '/pairs/1/backup', {}),
            ('POST', '/caregiver/relay/escalate', {'pair_id': '1'}),
            ('as', 'user'),
            ('POST', '/caregiver/relay/backup', {'pair_id': '1'}),
            ('POST', '/caregiver/pair/create', {'location_query': '都昌'}),
            ('GET', '/community/牛家垄周村'),
            ('as', 'guest'),
            ('POST', '/pairs/1/escalate', {}),
            ('POST', '/caregiver/pair/create', {'location_query': '都昌'}),
            ('POST', '/family-members/1/delete', {}),
            ('as', 'community'),
            ('GET', '/community/岭背徐村'),
        ],
    },
    {
        'name': 'S7-家庭成员与健康记录',
        'as': 'caregiver',
        'steps': [
            ('POST', '/family-members/new', {'name': '李爷爷', 'relation': '父亲', 'age': '81', 'gender': '男'}),
            ('let', 'member', _max_id('FamilyMember')),
            ('GET', '/family-members/{member}'),
            ('POST', '/family-members/{member}/edit',
             {'name': '李爷爷', 'relation': '父亲', 'age': '82', 'gender': '男', 'high_temp': '35',
              'low_temp': '5', 'high_humidity': '85', 'high_aqi': '150'}),
            ('POST', '/family-members/{member}/toggle-alert', {}),
            ('POST', '/medication-reminders',
             {'medicine_name': '降压药', 'dosage': '1片', 'frequency': '每日一次', 'time_of_day': '08:00',
              'member_id': '{member}'}),
            ('let', 'reminder', _max_id('MedicationReminder')),
            ('POST', '/medication-reminders/{reminder}/delete', {}),
            ('POST', '/health-diary',
             {'entry_date': '2026-07-20', 'symptoms': '头晕', 'severity': '2', 'notes': '午后', 'member_id': '{member}'}),
            ('POST', '/family-members/{member}/delete', {}),
            ('as', 'user'),
            ('POST', '/health-diary', {'entry_date': '2026-07-19', 'symptoms': '乏力', 'severity': '1', 'notes': ''}),
            ('POST', '/medication-reminders/1/delete', {}),
        ],
    },
    {
        'name': 'S8-管理后台增删改',
        'as': 'admin',
        'steps': [
            ('POST', '/admin/user/add',
             {'username': 'bl_added', 'password': 'Added-pass-2026', 'email': 'bl_added@example.org',
              'role': 'user', 'age': '66', 'gender': '女', 'community': '徐家湾'}),
            ('let', 'added_user', _max_id('User')),
            ('POST', '/admin/user/{added_user}/edit',
             {'username': 'bl_added', 'email': 'bl_added2@example.org', 'role': 'caregiver', 'age': '67',
              'gender': '女', 'community': '岭背徐村'}),
            ('POST', '/admin/user/{added_user}/delete', {}),
            ('POST', '/admin/community/add',
             {'name': '新增测试村', 'location': '都昌县', 'latitude': '29.30', 'longitude': '116.21',
              'population': '500', 'elderly_ratio': '0.3', 'chronic_disease_ratio': '0.2'}),
            ('let', 'added_community', _max_id('Community')),
            ('POST', '/admin/community/{added_community}/edit',
             {'name': '新增测试村', 'location': '都昌县', 'latitude': '29.31', 'longitude': '116.22',
              'population': '520', 'elderly_ratio': '0.31', 'chronic_disease_ratio': '0.21'}),
            ('POST', '/admin/cooling/add',
             {'community_code': '徐家湾', 'name': '祠堂纳凉点', 'resource_type': '纳凉点', 'address_hint': '祠堂',
              'open_hours': '09:00-18:00', 'has_ac': 'on', 'is_accessible': 'on', 'contact_hint': '村委',
              'notes': '', 'latitude': '29.33', 'longitude': '116.19'}),
            ('let', 'added_cooling', _max_id('CoolingResource')),
            ('POST', '/admin/cooling/{added_cooling}/edit',
             {'community_code': '徐家湾', 'name': '祠堂纳凉点', 'resource_type': '纳凉点', 'address_hint': '祠堂东侧',
              'open_hours': '09:00-19:00', 'is_active': 'on'}),
            ('POST', '/admin/communities/sync-coordinates', {}),
            ('GET', '/admin/users'),
            ('as', 'user'),
            ('POST', '/admin/user/1/delete', {}),
        ],
    },
    {
        'name': 'S9-个人资料与位置',
        'as': 'user',
        'steps': [
            ('POST', '/profile', {'form_id': 'basic', 'age': '73', 'gender': '女', 'community': '岭背徐村',
                                  'has_chronic_disease': 'on', 'chronic_diseases': ['高血压', '糖尿病'],
                                  'email': 'bl_user@example.org'}),
            ('POST', '/profile', {'form_id': 'password', 'old_password': 'wrong', 'new_password': 'New-pass-2026'}),
            ('POST', '/profile', {'form_id': 'api_token', 'token_name': '家里平板'}),
            ('POST', '/location', {'location': '徐家湾'}),
            ('GET', '/'),
            ('POST', '/health-assessment', {'age': '73', 'gender': '女'}),
            ('GET', '/profile'),
        ],
    },
    {
        'name': 'S11-仪表盘自动生成极端天气预警',
        'as': 'user',
        'steps': [
            ('set', '清空近一天预警并标记今日极端高温', _prepare_extreme_day),
            ('GET', '/dashboard'),
            ('GET', '/dashboard'),
            ('as', 'caregiver'),
            ('GET', '/dashboard'),
        ],
    },
    {
        'name': 'S12-社区统计口径',
        'as': 'community',
        'steps': [
            ('set', '构造当天多种状态的配对与一个空社区', _prepare_community_mix),
            ('GET', '/community/牛家垄周村'),
            ('GET', '/community'),
            ('GET', '/community/岭背徐村'),
            ('as', 'admin'),
            ('GET', '/community'),
            ('GET', '/community/岭背徐村'),
            ('GET', '/community/徐家湾'),
            ('as', 'caregiver'),
            ('GET', '/caregiver'),
            ('GET', '/pairs'),
        ],
    },
    {
        'name': 'S10-注册登录与游客',
        'as': 'guest',
        'steps': [
            ('POST', '/register', {'username': 'bl_newbie', 'password': 'Newbie-pass-2026',
                                   'email': 'bl_newbie@example.org', 'age': '70', 'gender': '男',
                                   'community': '徐家湾'}),
            ('POST', '/register', {'username': 'bl_user', 'password': 'Newbie-pass-2026',
                                   'email': 'dup@example.org'}),
            ('as', 'guest#2'),
            ('POST', '/login', {'username': 'bl_user', 'password': 'wrong-1'}),
            ('POST', '/login', {'username': 'bl_user', 'password': 'wrong-2'}),
            ('POST', '/login', {'username': 'bl_user', 'password': 'wrong-3'}),
            ('POST', '/login', {'username': 'bl_user', 'password': 'wrong-4'}),
            ('POST', '/login', {'username': 'bl_user', 'password': 'wrong-5'}),
            ('POST', '/login', {'username': 'bl_user', 'password': 'behavior-lock-pass'}),
            ('as', 'guest#3'),
            ('POST', '/login', {'username': 'bl_caregiver', 'password': 'behavior-lock-pass', 'remember': '1',
                                'next': '/pairs'}),
            ('GET', '/pairs'),
            ('POST', '/logout', {}),
            ('GET', '/pairs'),
            ('as', 'guest#4'),
            ('GET', '/guest'),
            ('GET', '/'),
            ('POST', '/location', {'location': '岭背徐村'}),
            ('POST', '/logout', {}),
        ],
    },
]
