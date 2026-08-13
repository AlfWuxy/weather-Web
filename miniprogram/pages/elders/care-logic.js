const { duchangDateKey, formatDateTime } = require('../../utils/format');

const FIXED_LOCATION = '都昌县';

const ASSESSMENT_QUESTIONS = [
  {
    id: 'outdoor_exposure',
    title: '今天在户外待了多久？',
    options: [
      { value: 'low', label: '主要在室内' },
      { value: 'medium', label: '约 1 到 3 小时' },
      { value: 'high', label: '超过 3 小时' },
    ],
  },
  {
    id: 'symptom_level',
    title: '现在有没有不舒服？',
    options: [
      { value: 'none', label: '无明显不适' },
      { value: 'mild', label: '轻微不适' },
      { value: 'moderate', label: '中等不适' },
      { value: 'severe', label: '明显或严重' },
    ],
  },
  {
    id: 'hydration',
    title: '今天喝水情况怎样？',
    options: [
      { value: 'good', label: '充足' },
      { value: 'normal', label: '一般' },
      { value: 'poor', label: '不足' },
    ],
  },
  {
    id: 'medication_adherence',
    title: '近期服药是否规律？',
    options: [
      { value: 'good', label: '规律' },
      { value: 'partial', label: '偶尔漏服' },
      { value: 'poor', label: '经常漏服' },
    ],
  },
  {
    id: 'sleep_quality',
    title: '最近睡眠质量怎样？',
    options: [
      { value: 'good', label: '较好' },
      { value: 'fair', label: '一般' },
      { value: 'poor', label: '较差' },
    ],
  },
];

function cleanText(value, maxLength) {
  const text = String(value == null ? '' : value).trim();
  return typeof maxLength === 'number' ? text.slice(0, maxLength) : text;
}

function splitChronic(text) {
  const seen = new Set();
  return String(text || '')
    .split(/[,，、]/)
    .map((item) => cleanText(item, 50))
    .filter((item) => {
      if (!item || seen.has(item)) return false;
      seen.add(item);
      return true;
    });
}

function validateElderInput(input, options) {
  const name = cleanText(input && input.name, 50);
  const relation = cleanText(input && input.relation, 20);
  const gender = cleanText(input && input.gender, 10);
  const rawAge = cleanText(input && input.age, 20);
  let age = null;

  if (!name) {
    return { valid: false, error: '请填写老人姓名或称呼' };
  }
  if (!rawAge) {
    return { valid: false, error: '请填写这位成年家人的年龄' };
  }
  age = Number(rawAge);
  if (!/^\d{1,3}$/.test(rawAge) || !Number.isInteger(age) || age < 18 || age > 120) {
    return { valid: false, error: '年龄请填写 18 到 120 的整数' };
  }
  if (gender && !['女性', '男性', '未填写'].includes(gender)) {
    return { valid: false, error: '请选择正确的性别' };
  }

  return {
    valid: true,
    payload: {
      name,
      relation,
      age,
      gender: gender === '未填写' ? '' : gender,
      location_query: FIXED_LOCATION,
      chronic_diseases: splitChronic(input && input.chronicText),
    },
  };
}

function formatLocalDate(date) {
  return duchangDateKey(date === undefined ? new Date() : date);
}

function isValidDateText(value) {
  const text = cleanText(value, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) return false;
  const [year, month, day] = text.split('-').map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
}

function validateDiaryInput(input) {
  const entryDate = cleanText(input && input.entryDate, 10);
  const severity = cleanText(input && input.severity, 20);
  const symptoms = cleanText(input && input.symptoms, 200);
  const notes = cleanText(input && input.notes, 500);
  if (!isValidDateText(entryDate)) {
    return { valid: false, error: '请选择正确的日期' };
  }
  if (!['轻微', '中等', '严重'].includes(severity)) {
    return { valid: false, error: '请选择不适程度' };
  }
  if (!symptoms) {
    return { valid: false, error: '请填写今天的身体状态或症状' };
  }
  return {
    valid: true,
    payload: {
      entry_date: entryDate,
      severity,
      symptoms,
      notes,
    },
  };
}

function parseOptionalNumber(value, label, min, max) {
  const text = cleanText(value, 20);
  if (!text) return { value: null };
  const number = Number(text);
  if (!Number.isFinite(number) || number < min || number > max) {
    return { error: `${label}应在 ${min} 到 ${max} 之间` };
  }
  return { value: number };
}

function validateMedicationInput(input) {
  const medicineName = cleanText(input && input.medicineName, 100);
  const dosage = cleanText(input && input.dosage, 100);
  const frequency = cleanText(input && input.frequency, 20) || 'daily';
  const timeOfDay = cleanText(input && input.timeOfDay, 5);
  if (!medicineName) return { valid: false, error: '请填写药品名称' };
  if (!['daily', 'weekly'].includes(frequency)) {
    return { valid: false, error: '请选择提醒频率' };
  }
  if (timeOfDay && !/^([01]\d|2[0-3]):[0-5]\d$/.test(timeOfDay)) {
    return { valid: false, error: '请选择正确的记录时间' };
  }

  const fields = [
    ['high_temp', input && input.highTemp, '高温阈值', -50, 60],
    ['low_temp', input && input.lowTemp, '低温阈值', -50, 60],
    ['high_humidity', input && input.highHumidity, '湿度阈值', 0, 100],
    ['high_aqi', input && input.highAqi, 'AQI 阈值', 0, 500],
  ];
  const weatherTriggers = {};
  for (const [key, value, label, min, max] of fields) {
    const parsed = parseOptionalNumber(value, label, min, max);
    if (parsed.error) return { valid: false, error: parsed.error };
    if (parsed.value !== null) weatherTriggers[key] = parsed.value;
  }
  if (
    weatherTriggers.low_temp !== undefined
    && weatherTriggers.high_temp !== undefined
    && weatherTriggers.low_temp > weatherTriggers.high_temp
  ) {
    return { valid: false, error: '低温阈值不能高于高温阈值' };
  }

  return {
    valid: true,
    payload: {
      medicine_name: medicineName,
      dosage,
      frequency,
      time_of_day: timeOfDay,
      weather_triggers: weatherTriggers,
    },
  };
}

function validateAssessment(answers) {
  const result = {};
  for (const question of ASSESSMENT_QUESTIONS) {
    const selected = cleanText(answers && answers[question.id], 20);
    const allowed = question.options.map((item) => item.value);
    if (!allowed.includes(selected)) {
      return { valid: false, error: '请完成全部 5 项筛查', missing: question.id };
    }
    result[question.id] = selected;
  }
  return { valid: true, payload: result };
}

function toFiniteNumber(value) {
  if (value === '' || value == null) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function pickFirst(source, keys) {
  if (!source || typeof source !== 'object') return undefined;
  for (const key of keys) {
    if (source[key] !== undefined && source[key] !== null && source[key] !== '') return source[key];
  }
  return undefined;
}

function hasOwn(source, key) {
  return Boolean(source && Object.prototype.hasOwnProperty.call(source, key));
}

function componentAliases(component) {
  return component === 'current' ? ['current', 'current_weather', 'weather'] : [component];
}

function snapshotComponentStatus(rootValue, metaValue, component, fallbackAvailable, nowMs) {
  const root = rootValue && typeof rootValue === 'object' ? rootValue : {};
  const meta = metaValue && typeof metaValue === 'object' ? metaValue : {};
  const sourceStatus = root.source_status && typeof root.source_status === 'object'
    && !Array.isArray(root.source_status)
    ? root.source_status
    : {};
  const aliases = componentAliases(component);
  const stateEntries = aliases
    .filter((key) => hasOwn(sourceStatus, key))
    .map((key) => ({ key, value: sourceStatus[key] }));
  const stateObjects = stateEntries
    .map((entry) => entry.value)
    .filter((value) => value && typeof value === 'object' && !Array.isArray(value));
  const stateContractValid = stateEntries.every((entry) => {
    const state = entry.value;
    const expiresValue = state && pickFirst(state, ['expires_at', 'expiresAt']);
    const hasValidExpiry = expiresValue !== undefined
      && Number.isFinite(Date.parse(String(expiresValue)));
    const hasStale = Boolean(state && hasOwn(state, 'stale'));
    return Boolean(
      state
      && typeof state === 'object'
      && !Array.isArray(state)
      && typeof state.available === 'boolean'
      && (!hasStale || typeof state.stale === 'boolean')
      && (expiresValue === undefined || hasValidExpiry)
      && (hasStale || hasValidExpiry)
    );
  });
  const stateAvailable = stateEntries.length === 0
    || (stateContractValid && stateObjects.every((state) => state.available === true));
  const componentPayload = aliases
    .map((key) => root[key])
    .find((value) => value && typeof value === 'object' && !Array.isArray(value));
  const payloadAvailable = componentPayload && hasOwn(componentPayload, 'available')
    ? componentPayload.available === true
    : true;
  const currentTime = Number.isFinite(Number(nowMs)) ? Number(nowMs) : Date.now();
  const expiryValues = stateObjects.map((state) => pickFirst(state, ['expires_at', 'expiresAt']));
  const parsedExpiries = expiryValues.map((value) => Date.parse(String(value || '')));
  const invalidExpiry = expiryValues.some((value, index) => value !== undefined && !Number.isFinite(parsedExpiries[index]));
  const expired = parsedExpiries.some((value) => Number.isFinite(value) && currentTime >= value);
  const staleKeys = aliases.map((key) => `${key}_stale`);
  const malformedRootStale = staleKeys.some((key) => hasOwn(root, key) && typeof root[key] !== 'boolean');
  const explicitRootStale = staleKeys.some((key) => root[key] === true);
  const explicitStateStale = stateObjects.some((state) => state.stale === true);
  const globalStale = meta.stale === true
    || cleanText(meta.source, 30) === 'stale-cache'
    || root.stale === true;
  const verifiedFreshState = stateEntries.length > 0
    && stateContractValid
    && stateObjects.every((state, index) => (
      state.available === true
      && state.stale === false
      && Number.isFinite(parsedExpiries[index])
      && currentTime < parsedExpiries[index]
    ));
  const stale = malformedRootStale
    || invalidExpiry
    || expired
    || explicitRootStale
    || explicitStateStale
    || (globalStale && !verifiedFreshState);
  const rootAvailabilityDeclared = hasOwn(root, 'available');
  const rootCurrentAvailable = !rootAvailabilityDeclared || root.available === true;
  const rootAvailable = component === 'warnings'
    ? (stateEntries.length > 0
      ? (root.available === true || verifiedFreshState)
      : root.available === true)
    : (['current', 'risk'].includes(component)
      ? rootCurrentAvailable
      : true);
  const available = Boolean(fallbackAvailable)
    && stateContractValid
    && stateAvailable
    && payloadAvailable
    && rootAvailable;
  const firstState = stateObjects[0] || {};
  const expiresValue = pickFirst(firstState, ['expires_at', 'expiresAt']);
  return {
    available,
    stale,
    usable: available && !stale,
    verified: verifiedFreshState,
    expiresAt: expiresValue === undefined ? '' : cleanText(expiresValue, 50),
    fetchedAt: cleanText(pickFirst(firstState, ['fetched_at', 'fetchedAt', 'updated_at']), 50),
  };
}

function warningTrigger(warnings) {
  const text = (Array.isArray(warnings) ? warnings : [])
    .map((item) => `${item && item.title ? item.title : ''}${item && item.type ? item.type : ''}`)
    .join(' ');
  if (/高温|heat/i.test(text)) return 'heat';
  if (/寒潮|低温|cold/i.test(text)) return 'cold';
  return '';
}

function normalizeSnapshot(snapshot) {
  const envelope = snapshot && typeof snapshot === 'object' ? snapshot : {};
  const meta = envelope.meta && typeof envelope.meta === 'object' ? envelope.meta : {};
  const first = envelope.data && typeof envelope.data === 'object' ? envelope.data : envelope;
  const root = first && first.snapshot && typeof first.snapshot === 'object' ? first.snapshot : (first || {});
  const current = root.current || root.current_weather || root.weather || {};
  const forecastSource = root.forecast || root.forecasts || root.daily || root.forecast_7day || [];
  const forecast = Array.isArray(forecastSource)
    ? forecastSource
    : (forecastSource && (forecastSource.items || forecastSource.daily)) || [];
  const today = (Array.isArray(forecast) && forecast[0]) || {};
  const todayMax = toFiniteNumber(pickFirst(today, ['temperature_max', 'tempMax', 'temp_max', 'max_temp']));
  const currentMax = toFiniteNumber(pickFirst(current, ['temperature_max', 'tempMax', 'temp_max', 'max_temp']));
  const todayMin = toFiniteNumber(pickFirst(today, ['temperature_min', 'tempMin', 'temp_min', 'min_temp']));
  const currentMin = toFiniteNumber(pickFirst(current, ['temperature_min', 'tempMin', 'temp_min', 'min_temp']));
  const tmax = todayMax !== null ? todayMax : currentMax;
  const tmin = todayMin !== null ? todayMin : currentMin;
  const temperature = toFiniteNumber(pickFirst(current, ['temperature', 'temp', 'tempNow', 'temp_now']));
  const humidity = toFiniteNumber(pickFirst(current, ['humidity', 'humidity_percent']));
  const warningsSource = root.warnings || root.alerts || root.warning || [];
  const warnings = Array.isArray(warningsSource)
    ? warningsSource
    : (warningsSource && (warningsSource.items || warningsSource.list)) || [];
  const currentStatus = snapshotComponentStatus(
    root,
    meta,
    'current',
    temperature !== null || tmax !== null || tmin !== null,
  );
  const warningsStatus = snapshotComponentStatus(root, meta, 'warnings', true);
  const riskSource = root.risk && typeof root.risk === 'object' ? root.risk : {};
  const riskFallback = riskSource.available === true
    || toFiniteNumber(pickFirst(riskSource, ['score', 'risk_score'])) !== null
    || Boolean(cleanText(pickFirst(riskSource, ['level', 'label', 'risk_level']), 30));
  const riskStatus = snapshotComponentStatus(root, meta, 'risk', riskFallback);
  const safeWarnings = warningsStatus.usable ? warnings : [];
  let trigger = warningTrigger(safeWarnings);
  if (!trigger && currentStatus.usable && tmax !== null && tmax >= 35) trigger = 'heat';
  else if (!trigger && currentStatus.usable && tmin !== null && tmin <= 5) trigger = 'cold';
  const available = currentStatus.available;
  const cacheSource = cleanText(meta.source, 30);
  const stale = available && currentStatus.stale;
  const freshnessState = !available ? 'unavailable' : (stale ? 'stale' : 'fresh');
  const rootUpdatedValue = pickFirst(root, ['updated_at', 'updatedAt', 'fetched_at', 'generated_at']);
  const componentUpdatedValue = currentStatus.fetchedAt || warningsStatus.fetchedAt;
  const updatedValue = componentUpdatedValue
    || (rootUpdatedValue !== undefined ? rootUpdatedValue : pickFirst(meta, ['storedAt']));
  const updatedAt = typeof updatedValue === 'number' && Number.isFinite(updatedValue)
    ? new Date(updatedValue).toISOString()
    : cleanText(updatedValue, 40);
  return {
    location: FIXED_LOCATION,
    temperature,
    temperatureMax: tmax,
    temperatureMin: tmin,
    humidity,
    condition: cleanText(pickFirst(current, ['condition', 'text', 'weather', 'weather_text']), 40),
    trigger,
    warnings: safeWarnings,
    updatedAt,
    updatedText: formatDateTime(updatedValue),
    available,
    stale,
    cacheSource,
    freshnessState,
    componentStatus: {
      current: currentStatus,
      warnings: warningsStatus,
      risk: riskStatus,
    },
  };
}

function markSnapshotStale(weather) {
  const source = weather && typeof weather === 'object' ? weather : normalizeSnapshot({});
  const available = Boolean(source.available);
  const componentStatus = Object.keys(source.componentStatus || {}).reduce((result, key) => {
    const status = source.componentStatus[key] || {};
    result[key] = {
      ...status,
      stale: Boolean(status.available),
      usable: false,
      verified: false,
    };
    return result;
  }, {});
  return {
    ...source,
    trigger: '',
    warnings: [],
    stale: available,
    cacheSource: available ? 'stale-cache' : (source.cacheSource || ''),
    freshnessState: available ? 'stale' : 'unavailable',
    componentStatus,
  };
}

function trustedWeatherTrigger(weather) {
  const source = weather && typeof weather === 'object' ? weather : {};
  const status = source.componentStatus && typeof source.componentStatus === 'object'
    ? source.componentStatus
    : {};
  const warnings = Array.isArray(source.warnings) ? source.warnings : [];
  const fromWarning = status.warnings && status.warnings.usable
    ? warningTrigger(warnings)
    : '';
  if (fromWarning) return fromWarning;
  if (!status.current || !status.current.usable) return '';
  const tmax = toFiniteNumber(source.temperatureMax);
  const tmin = toFiniteNumber(source.temperatureMin);
  if (tmax !== null && tmax >= 35) return 'heat';
  if (tmin !== null && tmin <= 5) return 'cold';
  return '';
}

function buildReminderMessage(input) {
  const relation = cleanText(input && input.relation, 20);
  const elderName = cleanText(input && input.elderName, 50);
  const trigger = cleanText(input && input.trigger, 20);
  const tmax = toFiniteNumber(input && input.tmax);
  const tmin = toFiniteNumber(input && input.tmin);
  let address = elderName || '您';
  if (['母亲', '妈妈', '妈'].includes(relation)) address = '妈';
  if (['父亲', '爸爸', '爸'].includes(relation)) address = '爸';

  let heading = `【都昌县天气提醒】${address}，今天也要照顾好自己。`;
  let advice = '出门前看看天气，随身带水或外套，按平时安排休息和服药。';
  if (trigger === 'heat') {
    heading = `【都昌县高温提醒】${address}，今天可能很热${tmax !== null ? `，最高约 ${tmax}°C` : ''}。`;
    advice = '请避开中午外出，少量多次喝水；室内开风扇或空调，感觉不舒服就先休息并告诉家人。';
  } else if (trigger === 'cold') {
    heading = `【都昌县低温提醒】${address}，今天可能较冷${tmin !== null ? `，最低约 ${tmin}°C` : ''}。`;
    advice = '请尽量减少外出，注意保暖和防滑；取暖时保持通风，避免烫伤。';
  }
  return [
    heading,
    advice,
    `地点：${FIXED_LOCATION}`,
    '这是行动提醒和状态筛查，不作医疗诊断。若出现胸痛、呼吸困难、意识异常、持续高热等严重症状，请立即联系家人并及时就医或求助。',
  ].join('\n');
}

function normalizeList(data, keys) {
  if (Array.isArray(data)) return data;
  const source = data && typeof data === 'object' ? data : {};
  for (const key of keys || []) {
    if (Array.isArray(source[key])) return source[key];
  }
  return [];
}

module.exports = {
  ASSESSMENT_QUESTIONS,
  FIXED_LOCATION,
  buildReminderMessage,
  cleanText,
  formatLocalDate,
  isValidDateText,
  markSnapshotStale,
  normalizeList,
  normalizeSnapshot,
  snapshotComponentStatus,
  splitChronic,
  trustedWeatherTrigger,
  validateAssessment,
  validateDiaryInput,
  validateElderInput,
  validateMedicationInput,
};
