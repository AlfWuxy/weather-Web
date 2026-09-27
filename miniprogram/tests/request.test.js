const test = require('node:test');
const assert = require('node:assert/strict');

const { createApiError, mapAbortable } = require('../utils/request');

test('API 错误保留安全的结构化字段', () => {
  const error = createApiError({
    statusCode: 428,
    header: { Authorization: 'redacted-test-value' },
    data: {
      success: false,
      error: 'privacy_consent_required',
      message: '请更新隐私同意',
      data: { required_version: '2026-07-17' },
    },
  });
  assert.equal(error.code, 'privacy_consent_required');
  assert.equal(error.message, '请更新隐私同意');
  assert.equal(error.statusCode, 428);
  assert.deepEqual(error.data, { required_version: '2026-07-17' });
  assert.equal(error.header, undefined);
  assert.equal(JSON.stringify(error).includes('redacted-test-value'), false);
});

test('401 保留 unauthorized code', () => {
  const error = createApiError({ statusCode: 401, data: {} });
  assert.equal(error.code, 'unauthorized');
});

test('GIS 响应映射保留底层请求的 abort 能力', async () => {
  let aborted = false;
  const pending = Promise.resolve({ value: 2 });
  pending.abort = () => { aborted = true; };
  const mapped = mapAbortable(pending, (value) => value.value * 2);
  mapped.abort();
  assert.equal(aborted, true);
  assert.equal(await mapped, 4);
});
