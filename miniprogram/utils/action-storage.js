const ACTIONS_STORAGE_KEY = 'yl_actions';
const LEGACY_ACTIONS_PREFIX = 'yl_actions_';

function normalizeChecked(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const checked = {};
  Object.keys(value).forEach((key) => {
    // 只恢复明确的布尔值，避免本机异常数据扩大页面状态。
    if (
      value[key] === true
      && key !== '__proto__'
      && key !== 'constructor'
      && key !== 'prototype'
    ) {
      checked[key] = true;
    }
  });
  return checked;
}

function legacyKeys(storage) {
  try {
    const info = storage.getStorageInfoSync();
    const keys = info && Array.isArray(info.keys) ? info.keys : [];
    return keys.filter((key) => String(key).startsWith(LEGACY_ACTIONS_PREFIX));
  } catch (error) {
    return [];
  }
}

function clearLegacyActionKeys(storage) {
  legacyKeys(storage).forEach((key) => {
    try {
      storage.removeStorageSync(key);
    } catch (error) {
      // 单个旧键清理失败不阻断当天清单加载，下次进入会重试。
    }
  });
}

function readStorage(storage, key) {
  try {
    return storage.getStorageSync(key);
  } catch (error) {
    return undefined;
  }
}

function loadActionChecked(storage, date) {
  const expectedDate = String(date || '').trim();
  const current = readStorage(storage, ACTIONS_STORAGE_KEY);
  const currentIsToday = Boolean(
    current
    && typeof current === 'object'
    && !Array.isArray(current)
    && current.date === expectedDate
    && current.checked
    && typeof current.checked === 'object'
    && !Array.isArray(current.checked)
  );
  const legacyToday = readStorage(storage, `${LEGACY_ACTIONS_PREFIX}${expectedDate}`);
  const checked = currentIsToday
    ? normalizeChecked(current.checked)
    : normalizeChecked(legacyToday);

  if (!currentIsToday) {
    storage.setStorageSync(ACTIONS_STORAGE_KEY, { date: expectedDate, checked });
  }
  // 新记录确认写入后再清理旧键，写失败时保留可恢复状态。
  clearLegacyActionKeys(storage);
  return checked;
}

function saveActionChecked(storage, date, value) {
  const envelope = {
    date: String(date || '').trim(),
    checked: normalizeChecked(value),
  };
  storage.setStorageSync(ACTIONS_STORAGE_KEY, envelope);
  clearLegacyActionKeys(storage);
  return envelope.checked;
}

module.exports = {
  ACTIONS_STORAGE_KEY,
  LEGACY_ACTIONS_PREFIX,
  clearLegacyActionKeys,
  loadActionChecked,
  saveActionChecked,
};
