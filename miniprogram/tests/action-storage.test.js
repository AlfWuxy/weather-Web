const test = require('node:test');
const assert = require('node:assert/strict');

const {
  ACTIONS_STORAGE_KEY,
  loadActionChecked,
  saveActionChecked,
} = require('../utils/action-storage');

function createStorage(entries) {
  const values = new Map(Object.entries(entries || {}));
  return {
    values,
    getStorageInfoSync() {
      return { keys: Array.from(values.keys()) };
    },
    getStorageSync(key) {
      return values.get(key);
    },
    setStorageSync(key, value) {
      values.set(key, value);
    },
    removeStorageSync(key) {
      values.delete(key);
    },
  };
}

test('升级时迁移当天旧键并清理所有 yl_actions_ 日期键', () => {
  const storage = createStorage({
    'yl_actions_2026-07-18': { water: true, room: false },
    'yl_actions_2026-07-17': { outdoor: true },
    unrelated: { keep: true },
  });

  const checked = loadActionChecked(storage, '2026-07-18');

  assert.deepEqual(checked, { water: true });
  assert.deepEqual(storage.values.get(ACTIONS_STORAGE_KEY), {
    date: '2026-07-18',
    checked: { water: true },
  });
  assert.equal(storage.values.has('yl_actions_2026-07-18'), false);
  assert.equal(storage.values.has('yl_actions_2026-07-17'), false);
  assert.deepEqual(storage.values.get('unrelated'), { keep: true });

  storage.values.set('yl_actions_2026-07-18', { room: true });
  assert.deepEqual(loadActionChecked(storage, '2026-07-18'), { water: true });
  assert.equal(storage.values.has('yl_actions_2026-07-18'), false);
});

test('日期变化时将单一 envelope 切换为当天空状态', () => {
  const storage = createStorage({
    [ACTIONS_STORAGE_KEY]: {
      date: '2026-07-17',
      checked: { water: true },
    },
    'yl_actions_2026-07-16': { room: true },
  });

  const checked = loadActionChecked(storage, '2026-07-18');

  assert.deepEqual(checked, {});
  assert.deepEqual(storage.values.get(ACTIONS_STORAGE_KEY), {
    date: '2026-07-18',
    checked: {},
  });
  assert.deepEqual(Array.from(storage.values.keys()), [ACTIONS_STORAGE_KEY]);
});

test('保存时只写入单一当天 envelope 并过滤非真值', () => {
  const storage = createStorage({
    'yl_actions_2026-07-15': { stale: true },
  });

  const checked = saveActionChecked(storage, '2026-07-18', {
    water: true,
    room: false,
    outdoor: 'true',
  });

  assert.deepEqual(checked, { water: true });
  assert.deepEqual(Array.from(storage.values.entries()), [[
    ACTIONS_STORAGE_KEY,
    { date: '2026-07-18', checked: { water: true } },
  ]]);
});

test('迁移写入失败时保留当天旧键供下次恢复', () => {
  const storage = createStorage({
    'yl_actions_2026-07-18': { water: true },
    'yl_actions_2026-07-17': { outdoor: true },
  });
  storage.setStorageSync = () => {
    throw new Error('storage quota exceeded');
  };

  assert.throws(
    () => loadActionChecked(storage, '2026-07-18'),
    /storage quota exceeded/,
  );
  assert.deepEqual(storage.values.get('yl_actions_2026-07-18'), { water: true });
  assert.deepEqual(storage.values.get('yl_actions_2026-07-17'), { outdoor: true });
  assert.equal(storage.values.has(ACTIONS_STORAGE_KEY), false);
});

test('当天保存失败时不提前清理历史状态', () => {
  const storage = createStorage({
    [ACTIONS_STORAGE_KEY]: {
      date: '2026-07-17',
      checked: { outdoor: true },
    },
    'yl_actions_2026-07-17': { outdoor: true },
  });
  storage.setStorageSync = () => {
    throw new Error('storage unavailable');
  };

  assert.throws(
    () => saveActionChecked(storage, '2026-07-18', { water: true }),
    /storage unavailable/,
  );
  assert.deepEqual(storage.values.get(ACTIONS_STORAGE_KEY), {
    date: '2026-07-17',
    checked: { outdoor: true },
  });
  assert.deepEqual(storage.values.get('yl_actions_2026-07-17'), { outdoor: true });
});
