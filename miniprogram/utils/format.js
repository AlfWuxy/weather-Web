function firstDefined(object, keys, fallback) {
  const source = object && typeof object === 'object' ? object : {};
  for (let index = 0; index < keys.length; index += 1) {
    const value = source[keys[index]];
    if (value !== undefined && value !== null && value !== '') return value;
  }
  return fallback;
}

function finiteNumber(value) {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function formatNumber(value, digits, suffix) {
  const number = finiteNumber(value);
  if (number === null) return '待更新';
  const precision = Number.isInteger(digits) ? digits : 0;
  return `${number.toFixed(precision)}${suffix || ''}`;
}

function formatTemperature(value) {
  return formatNumber(value, 0, '°');
}

function parseDate(value) {
  if (!value) return null;
  if (value instanceof Date && Number.isFinite(value.getTime())) return value;
  if (typeof value === 'number') {
    const date = new Date(value);
    return Number.isFinite(date.getTime()) ? date : null;
  }
  const text = String(value).trim();
  if (!text) return null;
  let normalized = /^\d{4}-\d{2}-\d{2} /.test(text) ? text.replace(' ', 'T') : text;
  if (/^\d{4}-\d{2}-\d{2}$/.test(normalized)) {
    normalized = `${normalized}T00:00:00+08:00`;
  } else if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/.test(normalized)) {
    // 后端旧数据若没有时区，按都昌县使用的中国标准时间解释。
    normalized = `${normalized}+08:00`;
  }
  const date = new Date(normalized);
  return Number.isFinite(date.getTime()) ? date : null;
}

function pad2(value) {
  return String(value).padStart(2, '0');
}

function duchangDateParts(date) {
  // 固定使用 UTC+8，避免设备或 CI 所在时区改变都昌县的更新时间。
  const shifted = new Date(date.getTime() + (8 * 60 * 60 * 1000));
  return {
    month: shifted.getUTCMonth() + 1,
    date: shifted.getUTCDate(),
    day: shifted.getUTCDay(),
    hours: shifted.getUTCHours(),
    minutes: shifted.getUTCMinutes(),
  };
}

function formatDateTime(value) {
  const date = parseDate(value);
  if (!date) return '更新时间未知';
  const parts = duchangDateParts(date);
  return `${pad2(parts.month)}月${pad2(parts.date)}日 ${pad2(parts.hours)}:${pad2(parts.minutes)}`;
}

function formatDay(value, index) {
  const date = parseDate(value);
  if (!date) return index === 0 ? '今天' : `第 ${index + 1} 天`;
  if (index === 0) return '今天';
  if (index === 1) return '明天';
  const week = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
  const parts = duchangDateParts(date);
  return `${parts.month}/${parts.date} ${week[parts.day]}`;
}

function riskTone(level, score) {
  const text = String(level || '').toLowerCase();
  if (/high|severe|danger|red|orange|高|严重|红|橙/.test(text)) return 'high';
  if (/mid|medium|moderate|yellow|中|较高|黄/.test(text)) return 'mid';
  if (/low|green|低|正常|绿/.test(text)) return 'low';
  const number = finiteNumber(score);
  if (number === null) return 'unknown';
  if (number >= 70) return 'high';
  if (number >= 40) return 'mid';
  return 'low';
}

function riskLabel(level, score, available) {
  if (available === false) return '风险待更新';
  const source = String(level || '').trim();
  if (source && !/^(high|mid|medium|moderate|low|unknown)$/i.test(source)) return source;
  const tone = riskTone(level, score);
  return { high: '高风险', mid: '需留意', low: '低风险', unknown: '风险待更新' }[tone];
}

function normalizeForecastItem(item, index) {
  const source = item && typeof item === 'object' ? item : {};
  const date = firstDefined(source, ['date', 'fxDate', 'forecast_date'], '');
  const high = finiteNumber(firstDefined(source, ['temp_high', 'temp_max', 'temperature_max', 'tempMax', 'high'], null));
  const low = finiteNumber(firstDefined(source, ['temp_low', 'temp_min', 'temperature_min', 'tempMin', 'low'], null));
  const score = finiteNumber(firstDefined(source, ['risk_score', 'score', 'composite_final_score'], null));
  const available = source.risk_available !== false && source.available !== false && score !== null;
  const level = firstDefined(source, ['risk_level', 'level', 'risk_label'], '');
  return {
    id: String(date || index),
    date,
    dayLabel: firstDefined(source, ['dow', 'day_label'], formatDay(date, index)),
    condition: String(firstDefined(source, ['condition', 'weather_condition', 'textDay', 'weather', 'text'], '天气待更新')),
    high,
    low,
    highText: formatTemperature(high),
    lowText: formatTemperature(low),
    humidity: finiteNumber(firstDefined(source, ['humidity', 'humidity_used'], null)),
    score,
    scoreText: score === null ? '待计算' : String(Math.round(score)),
    available,
    tone: riskTone(level, score),
    riskLabel: riskLabel(level, score, available),
    source,
  };
}

function normalizeWarning(item, index) {
  const source = item && typeof item === 'object' ? item : {};
  const level = String(firstDefined(source, ['level', 'severity', 'severityColor'], ''));
  return {
    id: String(firstDefined(source, ['id', 'warning_id', 'title'], index)),
    title: String(firstDefined(source, ['title', 'headline', 'typeName'], '天气预警')),
    type: String(firstDefined(source, ['type', 'eventType', 'typeName'], '')),
    level,
    tone: riskTone(level, null),
    text: String(firstDefined(source, ['text', 'description', 'detail'], '请留意官方最新信息。')),
    start: String(firstDefined(source, ['start_time', 'pubTime', 'effective'], '')),
    end: String(firstDefined(source, ['end_time', 'expireTime', 'expires'], '')),
  };
}

function normalizeAction(item, index) {
  if (typeof item === 'string') {
    return { id: `action-${index}`, title: item, detail: '', priority: index + 1 };
  }
  const source = item && typeof item === 'object' ? item : {};
  return {
    id: String(firstDefined(source, ['id', 'code'], `action-${index}`)),
    title: String(firstDefined(source, ['title', 'action', 'name'], '今日防护')),
    detail: String(firstDefined(source, ['detail', 'description', 'text', 'reason'], '')),
    priority: finiteNumber(firstDefined(source, ['priority', 'order'], index + 1)),
  };
}

function normalizeSources(sourceStatus) {
  if (Array.isArray(sourceStatus)) {
    return sourceStatus.map((item, index) => ({
      id: String(firstDefined(item, ['id', 'key', 'name'], index)),
      name: String(firstDefined(item, ['label', 'name', 'source'], '数据源')),
      status: String(firstDefined(item, ['status', 'message'], '状态未知')),
      updated: String(firstDefined(item, ['updated_at', 'fetched_at'], '')),
    }));
  }
  if (!sourceStatus || typeof sourceStatus !== 'object') return [];
  return Object.keys(sourceStatus).map((key) => {
    const value = sourceStatus[key];
    if (value && typeof value === 'object') {
      return {
        id: key,
        name: String(firstDefined(value, ['label', 'name'], key)),
        status: String(firstDefined(value, ['status', 'message', 'available'], '状态未知')),
        updated: String(firstDefined(value, ['updated_at', 'fetched_at'], '')),
      };
    }
    return { id: key, name: key, status: String(value), updated: '' };
  });
}

function warningSourceAvailable(sourceStatus) {
  if (!sourceStatus || typeof sourceStatus !== 'object' || Array.isArray(sourceStatus)) return false;
  const warnings = sourceStatus.warnings;
  if (typeof warnings === 'boolean') return warnings;
  if (warnings && typeof warnings === 'object' && typeof warnings.available === 'boolean') {
    return warnings.available;
  }
  // 来源状态缺失时无法确认确实没有预警，按不可用处理更安全。
  return false;
}

function normalizeBootstrap(payload) {
  const data = payload && typeof payload === 'object' ? payload : {};
  const currentSource = data.current && typeof data.current === 'object' ? data.current : {};
  const riskSource = data.risk && typeof data.risk === 'object' ? data.risk : {};
  const temperature = finiteNumber(firstDefined(currentSource, ['temperature', 'temp', 'temp_now', 'temperature_current'], null));
  const high = finiteNumber(firstDefined(currentSource, ['temperature_max', 'temp_max', 'tempMax', 'high'], null));
  const low = finiteNumber(firstDefined(currentSource, ['temperature_min', 'temp_min', 'tempMin', 'low'], null));
  const riskScore = finiteNumber(firstDefined(riskSource, ['score', 'risk_score', 'value', 'index'], null));
  const riskAvailable = data.available !== false && riskSource.available !== false && (riskScore !== null || Boolean(riskSource.level || riskSource.label));
  const riskLevelValue = firstDefined(riskSource, ['label', 'level', 'risk_level'], '');
  const forecastRaw = Array.isArray(data.forecast)
    ? data.forecast
    : (Array.isArray(data.forecast && data.forecast.days) ? data.forecast.days : []);
  const warningsRaw = Array.isArray(data.warnings)
    ? data.warnings
    : (Array.isArray(data.warnings && data.warnings.items) ? data.warnings.items : []);
  const actionsRaw = Array.isArray(data.actions)
    ? data.actions
    : (Array.isArray(data.actions && data.actions.items) ? data.actions.items : []);
  return {
    snapshotId: String(data.snapshot_id || ''),
    location: {
      name: String(firstDefined(data.location, ['name', 'label'], '都昌县')),
      code: String(firstDefined(data.location, ['code'], '')),
      scope: String(firstDefined(data.location, ['scope'], 'county')),
    },
    fetchedAt: data.fetched_at || data.generated_at || '',
    expiresAt: data.expires_at || '',
    ttlSeconds: finiteNumber(data.ttl_seconds) || 1800,
    available: data.available !== false,
    stale: Boolean(data.stale),
    current: {
      available: data.available !== false && currentSource.available !== false && temperature !== null,
      temperature,
      temperatureText: formatTemperature(temperature),
      high,
      highText: formatTemperature(high),
      low,
      lowText: formatTemperature(low),
      condition: String(firstDefined(currentSource, ['condition', 'weather_condition', 'text', 'weather'], '天气待更新')),
      humidity: finiteNumber(firstDefined(currentSource, ['humidity', 'humidity_pct'], null)),
      wind: String(firstDefined(currentSource, ['wind', 'wind_direction', 'wind_dir', 'windDir'], '')),
    },
    forecast: forecastRaw.map(normalizeForecastItem),
    warnings: warningsRaw.map(normalizeWarning),
    warningsSourceAvailable: warningSourceAvailable(data.source_status),
    risk: {
      available: riskAvailable,
      score: riskScore,
      scoreText: riskScore === null ? '待计算' : String(Math.round(riskScore)),
      level: String(riskLevelValue),
      label: riskLabel(riskLevelValue, riskScore, riskAvailable),
      tone: riskTone(riskLevelValue, riskScore),
      summary: String(firstDefined(riskSource, ['summary', 'message', 'reason'], Array.isArray(riskSource.reasons) ? riskSource.reasons.join('；') : '')),
    },
    actions: actionsRaw.map(normalizeAction),
    sources: normalizeSources(data.source_status),
    raw: data,
  };
}

function normalizeCommunityItem(item, index) {
  const source = item && typeof item === 'object' ? item : {};
  const hasRiskScore = ['risk_index', 'risk_score', 'score', 'normalized_score'].some((key) => source[key] !== undefined && source[key] !== null);
  const rawScore = finiteNumber(hasRiskScore
    ? firstDefined(source, ['risk_index', 'risk_score', 'score', 'normalized_score'], null)
    : source.vulnerability_index);
  const score = !hasRiskScore && rawScore !== null && rawScore <= 1 ? rawScore * 100 : rawScore;
  const available = source.available !== false && score !== null;
  const level = firstDefined(source, ['heatrisk_label', 'risk_label', 'risk_level', 'level'], '');
  const elderlyRatio = finiteNumber(firstDefined(source, ['elderly_ratio', 'age65_share', 'age65_share_pct'], null));
  const tone = riskTone(level, score);
  const vulnerabilityLabel = !available
    ? '待更新'
    : (tone === 'high' ? '高脆弱性' : tone === 'mid' ? '中等脆弱性' : '较低脆弱性');
  return {
    id: String(firstDefined(source, ['id', 'code', 'community_code', 'community', 'name'], index)),
    name: String(firstDefined(source, ['name', 'community', 'community_name'], '未命名社区')),
    code: String(firstDefined(source, ['code', 'community_code'], '')),
    score,
    scoreText: score === null ? '待更新' : String(Math.round(score)),
    available,
    tone,
    label: hasRiskScore ? riskLabel(level, score, available) : vulnerabilityLabel,
    metricLabel: hasRiskScore ? '综合风险指数' : '脆弱性指数',
    metricKind: hasRiskScore ? 'current-risk' : 'vulnerability',
    rank: finiteNumber(firstDefined(source, ['rank'], index + 1)),
    population: finiteNumber(firstDefined(source, ['population', 'total_population'], null)),
    elderlyRatio,
    elderlyText: elderlyRatio === null ? '待更新' : `${elderlyRatio <= 1 ? (elderlyRatio * 100).toFixed(1) : elderlyRatio.toFixed(1)}%`,
    uncertainty: finiteNumber(firstDefined(source, ['uncertainty_index'], null)),
    hotspot: String(firstDefined(source, ['hotspot_category'], '')),
    source,
  };
}

function normalizeCoolingItem(item, index) {
  const source = item && typeof item === 'object' ? item : {};
  return {
    id: String(firstDefined(source, ['id', 'code'], index)),
    name: String(firstDefined(source, ['name', 'title'], '避暑资源')),
    type: String(firstDefined(source, ['resource_type', 'type'], '避暑点')),
    community: String(firstDefined(source, ['community_name', 'community', 'community_code'], '都昌县')),
    address: String(firstDefined(source, ['address', 'address_hint', 'location'], '地址待维护')),
    hours: String(firstDefined(source, ['open_hours', 'hours'], '开放时间待确认')),
    phone: String(firstDefined(source, ['phone', 'contact_phone'], '')),
    contactHint: String(firstDefined(source, ['contact_hint'], '')),
    hasAc: source.has_ac === true,
    accessible: source.is_accessible === true || source.accessible === true,
    note: String(firstDefined(source, ['notes', 'note', 'description'], '')),
    latitude: finiteNumber(firstDefined(source, ['latitude', 'lat'], null)),
    longitude: finiteNumber(firstDefined(source, ['longitude', 'lng', 'lon'], null)),
  };
}

function normalizeCommunity(payload) {
  const data = payload && typeof payload === 'object' ? payload : {};
  const communitiesRaw = Array.isArray(data.communities)
    ? data.communities
    : (Array.isArray(data.items) ? data.items : []);
  const coolingRaw = Array.isArray(data.cooling)
    ? data.cooling
    : (Array.isArray(data.cooling_resources) ? data.cooling_resources : []);
  return {
    communities: communitiesRaw.map(normalizeCommunityItem),
    cooling: coolingRaw.map(normalizeCoolingItem),
    summary: data.summary && typeof data.summary === 'object' ? data.summary : {},
    gis: data.gis && typeof data.gis === 'object' ? data.gis : {},
    source: data.source && typeof data.source === 'object' ? data.source : {},
    generatedAt: data.generated_at || data.fetched_at || '',
    available: data.available !== false,
  };
}

function freshnessView(resultMeta, snapshot) {
  const meta = resultMeta || {};
  const data = snapshot || {};
  const storedAt = data.fetchedAt || data.generatedAt || meta.storedAt;
  return {
    updatedText: formatDateTime(storedAt),
    stale: Boolean(meta.stale || data.stale),
    source: meta.source || 'unknown',
    refreshDeferred: Boolean(meta.refreshDeferred),
  };
}

module.exports = {
  finiteNumber,
  firstDefined,
  formatDateTime,
  formatDay,
  formatNumber,
  formatTemperature,
  freshnessView,
  normalizeAction,
  normalizeBootstrap,
  normalizeCommunity,
  normalizeForecastItem,
  riskLabel,
  riskTone,
  warningSourceAvailable,
};
