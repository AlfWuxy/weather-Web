const test = require('node:test');
const assert = require('node:assert/strict');

const {
  ASSESSMENT_QUESTIONS,
  FIXED_LOCATION,
  buildReminderMessage,
  formatLocalDate,
  isValidDateText,
  normalizeSnapshot,
  splitChronic,
  validateAssessment,
  validateDiaryInput,
  validateElderInput,
  validateMedicationInput,
} = require('../pages/elders/care-logic');

test('慢病输入会按常见中文分隔符拆分并去重', () => {
  assert.deepEqual(splitChronic('高血压，糖尿病、高血压, 冠心病'), ['高血压', '糖尿病', '冠心病']);
});

test('老人资料固定为都昌县并验证年龄', () => {
  const invalid = validateElderInput({ name: '妈妈', age: '121' }, { mode: 'create' });
  assert.equal(invalid.valid, false);
  assert.match(invalid.error, /1 到 120/);

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
  assert.equal(formatLocalDate(new Date(2026, 6, 17, 23, 30)), '2026-07-17');
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
});
