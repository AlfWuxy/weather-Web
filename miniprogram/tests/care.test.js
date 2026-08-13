const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

let bootstrapResult = null;
let bootstrapOptions = null;
const publicDataPath = require.resolve('../utils/public-data');
require.cache[publicDataPath] = {
  id: publicDataPath,
  filename: publicDataPath,
  loaded: true,
  exports: {
    getBootstrap: async (options) => {
      bootstrapOptions = options;
      return bootstrapResult;
    },
  },
};

const careSession = require('../pages/elders/care-session');

const {
  ASSESSMENT_QUESTIONS,
  FIXED_LOCATION,
  buildReminderMessage,
  formatLocalDate,
  isValidDateText,
  markSnapshotStale,
  normalizeSnapshot,
  snapshotComponentStatus,
  splitChronic,
  trustedWeatherTrigger,
  validateAssessment,
  validateDiaryInput,
  validateElderInput,
  validateMedicationInput,
} = require('../pages/elders/care-logic');

test('慢病输入会按常见中文分隔符拆分并去重', () => {
  assert.deepEqual(splitChronic('高血压，糖尿病、高血压, 冠心病'), ['高血压', '糖尿病', '冠心病']);
});

test('老人资料固定为都昌县并验证年龄', () => {
  const missing = validateElderInput({ name: '妈妈', age: '' }, { mode: 'create' });
  assert.equal(missing.valid, false);
  assert.match(missing.error, /填写.*年龄/);

  const underage = validateElderInput({ name: '妹妹', age: '17' }, { mode: 'create' });
  assert.equal(underage.valid, false);
  assert.match(underage.error, /18 到 120/);

  const invalid = validateElderInput({ name: '妈妈', age: '121' }, { mode: 'create' });
  assert.equal(invalid.valid, false);
  assert.match(invalid.error, /18 到 120/);

  assert.equal(validateElderInput({ name: '异常家人', age: '1200' }, { mode: 'create' }).valid, false);
  assert.equal(validateElderInput({ name: '异常家人', age: '120.0' }, { mode: 'create' }).valid, false);

  assert.equal(validateElderInput({ name: '成年家人', age: '18' }, { mode: 'create' }).valid, true);
  assert.equal(validateElderInput({ name: '长寿家人', age: '120' }, { mode: 'create' }).valid, true);

  const valid = validateElderInput({
    name: ' 妈妈 ',
    relation: '母亲',
    age: '68',
    gender: '女性',
    chronicText: '高血压，糖尿病',
  }, { mode: 'create' });
  assert.equal(valid.valid, true);
  assert.equal(valid.payload.location_query, FIXED_LOCATION);
  assert.equal(valid.payload.age, 68);
  assert.deepEqual(valid.payload.chronic_diseases, ['高血压', '糖尿病']);
});

test('本地日期格式稳定且拒绝不存在的日期', () => {
  assert.equal(formatLocalDate(new Date('2026-07-17T23:30:00+08:00')), '2026-07-17');
  assert.equal(isValidDateText('2024-02-29'), true);
  assert.equal(isValidDateText('2025-02-29'), false);
  assert.equal(isValidDateText('2026-13-01'), false);
});

test('健康日记要求日期、程度及身体状态', () => {
  assert.equal(validateDiaryInput({ entryDate: '2026-07-17', severity: '轻微' }).valid, false);
  assert.equal(validateDiaryInput({ entryDate: '2026-07-17', severity: '轻微', notes: '只有备注' }).valid, false);
  const result = validateDiaryInput({
    entryDate: '2026-07-17',
    severity: '中等',
    symptoms: '下午有点头晕',
    notes: '',
  });
  assert.equal(result.valid, true);
  assert.equal(result.payload.symptoms, '下午有点头晕');
});

test('用药记录校验时间与天气阈值范围', () => {
  assert.equal(validateMedicationInput({ medicineName: '降压药', timeOfDay: '25:00' }).valid, false);
  assert.equal(validateMedicationInput({ medicineName: '降压药', highHumidity: '101' }).valid, false);
  assert.equal(validateMedicationInput({ medicineName: '降压药', lowTemp: '35', highTemp: '20' }).valid, false);
  const result = validateMedicationInput({
    medicineName: '降压药',
    dosage: '每次 1 片',
    frequency: 'daily',
    timeOfDay: '08:30',
    highTemp: '35',
  });
  assert.equal(result.valid, true);
  assert.deepEqual(result.payload.weather_triggers, { high_temp: 35 });
});

test('五项健康筛查必须全部完成且值在白名单内', () => {
  const answers = {};
  ASSESSMENT_QUESTIONS.forEach((question) => {
    answers[question.id] = question.options[0].value;
  });
  assert.equal(validateAssessment(answers).valid, true);
  delete answers.sleep_quality;
  assert.equal(validateAssessment(answers).valid, false);
  answers.sleep_quality = 'invalid';
  assert.equal(validateAssessment(answers).valid, false);
});

test('高温提醒包含都昌县和医疗安全提示', () => {
  const message = buildReminderMessage({
    trigger: 'heat',
    elderName: '李奶奶',
    relation: '',
    tmax: 36,
    tmin: 0,
  });
  assert.match(message, /李奶奶/);
  assert.match(message, /最高约 36°C/);
  assert.match(message, /地点：都昌县/);
  assert.match(message, /不作医疗诊断/);
  assert.match(message, /及时就医或求助/);
});

test('低温提醒保留 0°C，不会被空值逻辑吞掉', () => {
  const message = buildReminderMessage({
    trigger: 'cold',
    elderName: '爷爷',
    tmin: 0,
  });
  assert.match(message, /最低约 0°C/);
  const normalized = normalizeSnapshot({
    current: { temperature: 0, temperature_max: 4, temperature_min: 0 },
  });
  assert.equal(normalized.temperature, 0);
  assert.equal(normalized.temperatureMin, 0);
  assert.equal(normalized.available, true);
});

test('共享 bootstrap 快照可归一化且不需要逐老人天气', () => {
  const snapshot = {
    current: { temperature: 34, temperature_max: 36, temperature_min: 27, humidity: 70 },
    warnings: [{ title: '都昌县高温橙色预警' }],
    fetched_at: '2026-07-17T12:00:00+08:00',
  };
  const normalized = normalizeSnapshot(snapshot);
  assert.equal(normalized.available, true);
  assert.equal(normalized.trigger, 'heat');
  assert.equal(normalized.temperatureMax, 36);
  assert.equal(normalized.location, '都昌县');
  assert.equal(normalized.freshnessState, 'fresh');
  assert.equal(normalized.stale, false);
  assert.equal(normalized.updatedText, '07月17日 12:00');
});

test('照护快照保留缓存元数据并兼容 data 内的隐私版本', async () => {
  bootstrapResult = {
    data: {
      privacy: { required_version: 'privacy-v5' },
      auth: { required_privacy_consent_version: 'auth-v6' },
    },
    meta: { source: 'stale-cache', stale: true, storedAt: Date.parse('2026-07-17T12:00:00+08:00') },
  };
  bootstrapOptions = null;

  const result = await careSession.getSnapshot({ force: true });

  assert.equal(result, bootstrapResult);
  assert.deepEqual(bootstrapOptions, { force: true });
  assert.equal(careSession.extractRequiredPrivacyVersion(result, 'bundle-v1'), 'privacy-v5');
  assert.equal(careSession.extractRequiredPrivacyVersion({
    data: { auth: { required_version: 'auth-only-v7' } },
  }, 'bundle-v1'), 'auth-only-v7');
});

test('照护天气明确区分 fresh、stale 和 unavailable', () => {
  const payload = {
    current: { temperature: 34, temperature_max: 36, temperature_min: 27 },
    fetched_at: '2026-07-17T12:00:00+08:00',
  };
  const fresh = normalizeSnapshot({ data: payload, meta: { source: 'network', stale: false } });
  const stale = normalizeSnapshot({ data: payload, meta: { source: 'stale-cache', stale: true } });
  const unavailable = normalizeSnapshot({
    data: {},
    meta: { source: 'stale-cache', stale: true, storedAt: Date.parse('2026-07-17T12:00:00+08:00') },
  });

  assert.equal(fresh.freshnessState, 'fresh');
  assert.equal(fresh.stale, false);
  assert.equal(fresh.updatedText, '07月17日 12:00');
  assert.equal(stale.freshnessState, 'stale');
  assert.equal(stale.stale, true);
  assert.equal(stale.temperature, 34);
  assert.equal(unavailable.freshnessState, 'unavailable');
  assert.equal(unavailable.available, false);
  assert.equal(unavailable.stale, false);
  assert.equal(unavailable.updatedText, '07月17日 12:00');
});

test('照护组件支持当前天气别名并严格校验 available', () => {
  ['current', 'current_weather', 'weather'].forEach((alias) => {
    const normalized = normalizeSnapshot({
      data: {
        available: true,
        stale: true,
        [alias]: { temperature: 36, temperature_max: 38, temperature_min: 28 },
        risk: { available: true, level: '高风险' },
        source_status: {
          [alias]: {
            available: true,
            stale: false,
            expires_at: '2099-01-01T00:00:00Z',
          },
          risk: {
            available: true,
            stale: false,
            expires_at: '2099-01-01T00:00:00Z',
          },
        },
      },
      meta: { source: 'stale-cache', stale: true },
    });

    assert.equal(normalized.componentStatus.current.usable, true, alias);
    assert.equal(normalized.componentStatus.risk.usable, true, alias);
    assert.equal(trustedWeatherTrigger(normalized), 'heat', alias);
  });

  const malformedStates = [
    { available: 'false', stale: false, expires_at: '2099-01-01T00:00:00Z' },
    { stale: false, expires_at: '2099-01-01T00:00:00Z' },
  ];
  malformedStates.forEach((state) => {
    const root = {
      available: true,
      current: { temperature: 36, temperature_max: 38, temperature_min: 28 },
      warnings: [{ title: '来源不明的高温预警' }],
      source_status: { current: state, warnings: state },
    };
    assert.equal(
      snapshotComponentStatus(root, { source: 'network', stale: false }, 'current', true).usable,
      false,
    );
    assert.equal(
      snapshotComponentStatus(root, { source: 'network', stale: false }, 'warnings', true).usable,
      false,
    );
  });
});

test('旧缓存只有精确布尔状态和未来过期时间才能解锁组件', () => {
  const malformedStates = [
    { available: true },
    { available: true, expires_at: '不是日期' },
    { available: 'true', expires_at: '2099-01-01T00:00:00Z' },
    { expires_at: '2099-01-01T00:00:00Z' },
  ];

  malformedStates.forEach((state) => {
    const root = {
      available: true,
      stale: true,
      current: { temperature: 36, temperature_max: 38, temperature_min: 28 },
      warnings: [{ title: '旧高温预警' }],
      source_status: { current: state, warnings: state },
    };
    const normalized = normalizeSnapshot({ data: root, meta: { source: 'stale-cache', stale: true } });
    assert.equal(normalized.componentStatus.current.usable, false);
    assert.equal(normalized.componentStatus.warnings.usable, false);
    assert.deepEqual(normalized.warnings, []);
    assert.equal(trustedWeatherTrigger(normalized), '');
  });
});

test('新鲜官方预警可在当前天气不可用时独立触发照护提醒', () => {
  const normalized = normalizeSnapshot({
    data: {
      available: false,
      stale: true,
      current_stale: true,
      risk_stale: true,
      warnings_stale: false,
      current: {},
      risk: { available: false, level: '未知' },
      warnings: [{ title: '都昌县高温橙色预警' }],
      source_status: {
        current: { available: false, expires_at: '2099-01-01T00:00:00Z' },
        risk: { available: false, expires_at: '2099-01-01T00:00:00Z' },
        warnings: { available: true, stale: false, expires_at: '2099-01-01T00:00:00Z' },
      },
    },
    meta: { source: 'stale-cache', stale: true },
  });

  assert.equal(normalized.available, false);
  assert.equal(normalized.componentStatus.warnings.usable, true);
  assert.equal(normalized.warnings.length, 1);
  assert.equal(normalized.trigger, 'heat');
  assert.equal(trustedWeatherTrigger(normalized), 'heat');
});

test('根快照不可用时预警必须有显式精确来源状态', () => {
  [false, undefined].forEach((rootAvailable) => {
    const data = {
      warnings: [{ title: '无来源状态的高温预警' }],
    };
    if (rootAvailable !== undefined) data.available = rootAvailable;
    const legacy = normalizeSnapshot({
      data,
      meta: { source: 'network', stale: false },
    });
    assert.equal(legacy.componentStatus.warnings.usable, false);
    assert.deepEqual(legacy.warnings, []);
    assert.equal(trustedWeatherTrigger(legacy), '');
  });

  const explicit = normalizeSnapshot({
    data: {
      available: false,
      warnings: [{ title: '都昌县高温橙色预警' }],
      source_status: {
        warnings: { available: true, stale: false, expires_at: '2099-01-01T00:00:00Z' },
      },
    },
    meta: { source: 'stale-cache', stale: true },
  });
  assert.equal(explicit.componentStatus.warnings.usable, true);
  assert.equal(trustedWeatherTrigger(explicit), 'heat');
});

test('根快照明确不可用时旧 current 无来源状态也不能解锁', () => {
  const normalized = normalizeSnapshot({
    data: {
      available: false,
      current: { temperature: 38, temperature_max: 40 },
    },
    meta: { source: 'network', stale: false },
  });

  assert.equal(normalized.componentStatus.current.usable, false);
  assert.equal(normalized.available, false);
  assert.equal(trustedWeatherTrigger(normalized), '');
});

test('刷新失败保留旧天气并降级为 stale，空天气保持 unavailable', () => {
  const fresh = normalizeSnapshot({
    current: { temperature: 34, temperature_max: 36, temperature_min: 27 },
    fetched_at: '2026-07-17T12:00:00+08:00',
  });
  const retained = markSnapshotStale(fresh);
  const empty = markSnapshotStale(normalizeSnapshot({}));

  assert.equal(retained.temperature, 34);
  assert.equal(retained.updatedText, '07月17日 12:00');
  assert.equal(retained.freshnessState, 'stale');
  assert.equal(retained.stale, true);
  assert.equal(empty.freshnessState, 'unavailable');
  assert.equal(empty.stale, false);
});

test('照护页刷新失败后不把旧天气或缺失天气显示为绿色安全态', async () => {
  const careSessionPath = require.resolve('../pages/elders/care-session');
  const carePagePath = require.resolve('../pages/elders/index');
  const originalCareSessionModule = require.cache[careSessionPath];
  const originalPage = global.Page;
  let pageDefinition = null;
  let rejectSnapshot = false;
  let rejectElders = false;
  const snapshot = {
    data: {
      current: { temperature: 34, temperature_max: 36, temperature_min: 27 },
      fetched_at: '2026-07-17T12:00:00+08:00',
    },
    meta: { source: 'network', stale: false },
  };

  require.cache[careSessionPath] = {
    id: careSessionPath,
    filename: careSessionPath,
    loaded: true,
    exports: {
      authApi: async () => {
        if (rejectElders) throw new Error('offline');
        return {
          items: [{
            pair_id: 7,
            member: { name: '奶奶', relation: '祖母', age: 72 },
            today: { status_date: '2026-07-18', help_flag: true },
          }],
        };
      },
      getSnapshot: async () => {
        if (rejectSnapshot) throw new Error('offline');
        return snapshot;
      },
      requireToken: () => 'session-token',
    },
  };
  global.Page = (definition) => { pageDefinition = definition; };
  delete require.cache[carePagePath];

  function makePage() {
    const page = Object.assign({}, pageDefinition);
    page.data = Object.assign({}, pageDefinition.data, {
      elders: [],
      weather: normalizeSnapshot({}),
    });
    page.setData = (next) => Object.assign(page.data, next);
    return page;
  }

  try {
    require(carePagePath);
    const page = makePage();
    await page.loadCareHome.call(page);
    assert.equal(page.data.weather.freshnessState, 'fresh');

    rejectSnapshot = true;
    await page.loadCareHome.call(page);
    assert.equal(page.data.weather.temperature, 34);
    assert.equal(page.data.weather.freshnessState, 'stale');
    assert.equal(Object.hasOwn(page.data.elders[0], 'today'), false);

    const emptyPage = makePage();
    await emptyPage.loadCareHome.call(emptyPage);
    assert.equal(emptyPage.data.weather.freshnessState, 'unavailable');

    rejectSnapshot = false;
    await page.loadCareHome.call(page);
    rejectElders = true;
    await page.loadCareHome.call(page);
    assert.equal(page.data.weather.temperature, 34);
    assert.equal(page.data.weather.freshnessState, 'stale');
    assert.match(page.data.loadError, /上次成功加载/);

    const view = fs.readFileSync(path.join(__dirname, '..', 'pages/elders/index.wxml'), 'utf8');
    assert.match(view, /freshnessState === 'unavailable'[^>]*>风险待更新</);
    assert.match(view, /safe" wx:elif="{{weather\.freshnessState === 'fresh'}}">日常留意</);
    assert.doesNotMatch(view, /safe" wx:else>日常留意/);
  } finally {
    global.Page = originalPage;
    delete require.cache[carePagePath];
    require.cache[careSessionPath] = originalCareSessionModule;
  }
});
