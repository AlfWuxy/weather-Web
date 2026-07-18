const test = require('node:test');
const assert = require('node:assert/strict');

let pageDefinition;
let authApiImpl = async () => ({});
let modalOptions = null;
let toasts = [];

global.Page = (definition) => { pageDefinition = definition; };
global.wx = {
  redirectTo: () => {},
  showModal: (options) => { modalOptions = options; },
  showToast: (options) => { toasts.push(options); },
};

const careSessionPath = require.resolve('../pages/elders/care-session');
require.cache[careSessionPath] = {
  id: careSessionPath,
  filename: careSessionPath,
  loaded: true,
  exports: {
    authApi: (options) => authApiImpl(options),
    requireToken: () => 'session-token',
  },
};

function loadPage(relativePath) {
  const modulePath = require.resolve(relativePath);
  delete require.cache[modulePath];
  pageDefinition = null;
  require(modulePath);
  return pageDefinition;
}

function makePage(definition, overrides) {
  const page = Object.assign({}, definition);
  page.data = Object.assign({}, JSON.parse(JSON.stringify(definition.data)), overrides || {});
  page._mutations = 0;
  page.setData = (next, callback) => {
    page._mutations += 1;
    Object.assign(page.data, next);
    if (typeof callback === 'function') callback();
  };
  return page;
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

test('日记 POST 返回权威对象后只发一个请求', async () => {
  const requests = [];
  authApiImpl = async (options) => {
    requests.push(options);
    return {
      entry: {
        id: 22,
        entry_date: '2026-07-18',
        severity: '轻微',
        symptoms: '状态正常',
        notes: '',
      },
    };
  };
  toasts = [];
  const definition = loadPage('../pages/diary/index');
  const page = makePage(definition, {
    pairId: 7,
    contextReady: true,
    entryDate: '2026-07-18',
    todayDate: '2026-07-18',
    severity: '轻微',
    symptoms: '状态正常',
    entries: [{ id: 9, symptoms: '旧记录' }],
  });
  page._unloaded = false;
  page._lifecycleGeneration = 1;

  await page.saveEntry.call(page);

  assert.deepEqual(requests.map((item) => item.method), ['POST']);
  assert.equal(page.data.entries[0].id, 22);
  assert.equal(page.data.busy, false);
  assert.equal(toasts.at(-1).title, '日记已保存');
});

test('日记 POST 缺少记录时回退两个并行 GET', async () => {
  const requests = [];
  authApiImpl = async (options) => {
    requests.push(options);
    if (options.method === 'POST') return { ok: true };
    if (options.path === '/mp/api/v1/elders') {
      return { items: [{ pair_id: 7, member: { name: '奶奶' } }] };
    }
    return { items: [{ id: 23, entry_date: '2026-07-18', severity: '轻微', symptoms: '已同步' }] };
  };
  const definition = loadPage('../pages/diary/index');
  const page = makePage(definition, {
    pairId: 7,
    contextReady: true,
    entryDate: '2026-07-18',
    todayDate: '2026-07-18',
    severity: '轻微',
    symptoms: '状态正常',
  });
  page._unloaded = false;
  page._lifecycleGeneration = 1;

  await page.saveEntry.call(page);

  assert.equal(requests.length, 3);
  assert.equal(requests.filter((item) => item.method === 'GET').length, 2);
  assert.equal(page.data.entries[0].id, 23);
});

test('用药新增与权威删除都不会立即重载', async () => {
  const requests = [];
  authApiImpl = async (options) => {
    requests.push(options);
    if (options.method === 'POST') {
      return {
        medication: {
          id: 31,
          medicine_name: '测试药品',
          dosage: '一片',
          frequency: 'daily',
          time_of_day: '08:00',
          weather_triggers: {},
          is_active: true,
        },
      };
    }
    if (options.method === 'DELETE') return { deleted_id: 31 };
    throw new Error('unexpected_get');
  };
  modalOptions = null;
  const definition = loadPage('../pages/medications/index');
  const page = makePage(definition, {
    pairId: 7,
    contextReady: true,
    medicineName: '测试药品',
    dosage: '一片',
    frequency: 'daily',
    timeOfDay: '08:00',
  });
  page._unloaded = false;
  page._lifecycleGeneration = 1;

  await page.addMedication.call(page);
  assert.deepEqual(requests.map((item) => item.method), ['POST']);
  assert.equal(page.data.medications[0].id, 31);

  page.deleteMedication.call(page, {
    currentTarget: { dataset: { id: 31, name: '测试药品' } },
  });
  await modalOptions.success({ confirm: true });

  assert.deepEqual(requests.map((item) => item.method), ['POST', 'DELETE']);
  assert.deepEqual(page.data.medications, []);
});

test('用药新增或删除缺少权威结果时回退重新加载', async () => {
  const requests = [];
  authApiImpl = async (options) => {
    requests.push(options);
    if (options.method === 'POST' || options.method === 'DELETE') return { ok: true };
    if (options.path === '/mp/api/v1/elders') {
      return { items: [{ pair_id: 7, member: { name: '奶奶' } }] };
    }
    const deleteStarted = requests.some((item) => item.method === 'DELETE');
    return deleteStarted ? { items: [] } : {
      items: [{
        id: 32,
        medicine_name: '回退同步药品',
        dosage: '一片',
        frequency: 'daily',
        time_of_day: '08:00',
        weather_triggers: {},
      }],
    };
  };
  modalOptions = null;
  const definition = loadPage('../pages/medications/index');
  const page = makePage(definition, {
    pairId: 7,
    contextReady: true,
    medicineName: '回退同步药品',
    dosage: '一片',
    frequency: 'daily',
    timeOfDay: '08:00',
  });
  page._unloaded = false;
  page._lifecycleGeneration = 1;

  await page.addMedication.call(page);
  assert.deepEqual(requests.map((item) => item.method), ['POST', 'GET', 'GET']);
  assert.equal(page.data.medications[0].id, 32);

  page.deleteMedication.call(page, {
    currentTarget: { dataset: { id: 32, name: '回退同步药品' } },
  });
  await modalOptions.success({ confirm: true });

  assert.deepEqual(
    requests.map((item) => item.method),
    ['POST', 'GET', 'GET', 'DELETE', 'GET', 'GET']
  );
  assert.deepEqual(page.data.medications, []);
});

test('指定家人的筛查页并行请求列表和最近结果', async () => {
  const eldersGate = deferred();
  const latestGate = deferred();
  const requests = [];
  authApiImpl = (options) => {
    requests.push(options);
    return options.path === '/mp/api/v1/elders' ? eldersGate.promise : latestGate.promise;
  };
  const definition = loadPage('../pages/health-assessment/index');
  const page = makePage(definition);
  page._unloaded = false;
  page._latestRequestToken = 0;
  page._pageRequestToken = 0;
  page._submitRequestToken = 0;
  page.requestedPairId = 7;

  const pending = page.loadPage.call(page);
  await Promise.resolve();
  assert.equal(requests.length, 2);
  assert.deepEqual(new Set(requests.map((item) => item.path)), new Set([
    '/mp/api/v1/elders',
    '/mp/api/v1/health/assessment?pair_id=7',
  ]));

  latestGate.resolve({ latest: { id: 41, risk_level: '低风险', recommendations: [] } });
  await Promise.resolve();
  assert.equal(page.data.latest, null);
  eldersGate.resolve({ items: [{ pair_id: 7, member: { name: '奶奶' } }] });
  await pending;

  assert.equal(page.data.latest.id, 41);
  assert.equal(page.data.latestLoading, false);
  assert.equal(page.data.loading, false);
  assert.equal(requests.length, 2);
});

test('并行历史请求失败不阻断筛查，卸载后结果不回写', async () => {
  toasts = [];
  const definition = loadPage('../pages/health-assessment/index');
  const page = makePage(definition);
  page._unloaded = false;
  page._latestRequestToken = 0;
  page._pageRequestToken = 0;
  page._submitRequestToken = 0;
  page.requestedPairId = 7;
  authApiImpl = async (options) => {
    if (options.path === '/mp/api/v1/elders') {
      return { items: [{ pair_id: 7, member: { name: '奶奶' } }] };
    }
    throw new Error('history_offline');
  };

  await page.loadPage.call(page);
  assert.equal(page.data.pairId, 7);
  assert.match(page.data.latestError, /仍可继续/);
  assert.deepEqual(toasts, []);

  const eldersGate = deferred();
  const latestGate = deferred();
  authApiImpl = (options) => (
    options.path === '/mp/api/v1/elders' ? eldersGate.promise : latestGate.promise
  );
  const pending = page.loadPage.call(page);
  await Promise.resolve();
  page.onUnload.call(page);
  const mutationsAtUnload = page._mutations;
  eldersGate.resolve({ items: [{ pair_id: 7, member: { name: '奶奶' } }] });
  latestGate.resolve({ latest: { id: 42, risk_level: '高风险' } });
  await pending;

  assert.equal(page._mutations, mutationsAtUnload);
  assert.notEqual(page.data.latest && page.data.latest.id, 42);
});
