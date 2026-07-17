const test = require('node:test');
const assert = require('node:assert/strict');

const {
  formatDateTime,
  freshnessView,
  normalizeBootstrap,
  normalizeCommunity,
} = require('../utils/format');

test('bootstrap 兼容正式天气字段与 reasons', () => {
  const result = normalizeBootstrap({
    available: true,
    fetched_at: '2026-07-17T08:00:00+08:00',
    current: { temperature: 35, weather_condition: '晴', wind_dir: '南风' },
    risk: { score: 72, reasons: ['高温', '热夜'] },
    forecast: [{ date: '2026-07-18', temperature_max: 36, temperature_min: 28, weather_condition: '多云', risk_score: 70, risk_level: '高风险' }],
  });
  assert.equal(result.current.condition, '晴');
  assert.equal(result.current.wind, '南风');
  assert.equal(result.risk.summary, '高温；热夜');
  assert.equal(result.forecast[0].condition, '多云');
});

test('更新时间优先采用服务端真实抓取时间', () => {
  const view = freshnessView(
    { storedAt: Date.parse('2026-07-17T10:00:00+08:00') },
    { fetchedAt: '2026-07-17T08:00:00+08:00' }
  );
  assert.match(view.updatedText, /08:00/);
});

test('都昌县时间展示不受运行环境时区影响', () => {
  assert.equal(formatDateTime('2026-07-17T00:00:00Z'), '07月17日 08:00');
  assert.equal(formatDateTime('2026-07-17 08:00:00'), '07月17日 08:00');
});

test('预警列表为空时区分暂无预警与来源不可用', () => {
  const unavailable = normalizeBootstrap({
    warnings: [],
    source_status: { warnings: { available: false } },
  });
  assert.equal(unavailable.warnings.length, 0);
  assert.equal(unavailable.warningsSourceAvailable, false);

  const noWarnings = normalizeBootstrap({
    warnings: [],
    source_status: { warnings: { available: true } },
  });
  assert.equal(noWarnings.warnings.length, 0);
  assert.equal(noWarnings.warningsSourceAvailable, true);

  const unknownSource = normalizeBootstrap({ warnings: [] });
  assert.equal(unknownSource.warningsSourceAvailable, false);
});

test('预警保留发布单位、发布时间和生效时间', () => {
  const result = normalizeBootstrap({
    warnings: [{
      title: '高温黄色预警',
      start_time: '2026-07-17T08:30:00+08:00',
      raw: {
        sender: '都昌县气象台',
        pubTime: '2026-07-17T08:00:00+08:00',
        effectiveTime: '2026-07-17T08:30:00+08:00',
        expireTime: '2026-07-17T20:00:00+08:00',
      },
    }],
  });
  const warning = result.warnings[0];
  assert.equal(warning.issuer, '都昌县气象台');
  assert.equal(warning.issuedAt, '2026-07-17T08:00:00+08:00');
  assert.equal(warning.effectiveAt, '2026-07-17T08:30:00+08:00');
  assert.equal(warning.expiresAt, '2026-07-17T20:00:00+08:00');
  assert.match(warning.issuedText, /08:00/);
  assert.match(warning.effectiveText, /08:30/);

  const objectSender = normalizeBootstrap({
    warnings: [{ raw: { sender: { name: '九江市气象台' } } }],
  });
  assert.equal(objectSender.warnings[0].issuer, '九江市气象台');

  const publishedOnly = normalizeBootstrap({
    warnings: [{
      start_time: '2026-07-17T08:00:00+08:00',
      raw: { pubTime: '2026-07-17T08:00:00+08:00' },
    }],
  });
  assert.equal(publishedOnly.warnings[0].issuedText, '07月17日 08:00');
  assert.equal(publishedOnly.warnings[0].effectiveText, '未提供');
});

test('0 到 1 脆弱性指数转换为 0 到 100 显示', () => {
  const result = normalizeCommunity({
    communities: [{ name: '测试社区', vulnerability_index: 0.43 }],
    cooling: [{ name: '社区中心', address_hint: '中心附近', contact_hint: '请联系网格员' }],
  });
  assert.equal(result.communities[0].score, 43);
  assert.equal(result.communities[0].metricLabel, '脆弱性指数');
  assert.equal(result.communities[0].metricKind, 'vulnerability');
  assert.equal(result.communities[0].label, '中等脆弱性');
  assert.equal(result.cooling[0].address, '中心附近');
  assert.equal(result.cooling[0].contactHint, '请联系网格员');
  assert.equal(result.cooling[0].phone, '');
});
