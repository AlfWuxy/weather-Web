/* 浏览器实际使用的雨量呈现函数回归，覆盖缺失与时段口径。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const {summarize} = require('../static/js/rain-card.js');
const now = Date.parse('2026-10-06T09:30:00+08:00');
const sample = () => ({success: true, data: {available: true, source: 'Open-Meteo', timezone: 'Asia/Shanghai', precipitation_unit: 'mm', rainfall_interval_minutes: 60, rainfall_time_reference: 'interval_end', timeline: [
    {time: '2026-10-06T10:00:00+08:00', rainfall_mm: 2, precipitation_mm: 9, precipitation_probability: 90},
    {time: '2026-10-06T11:00:00+08:00', rainfall_mm: 8, precipitation_mm: 15, precipitation_probability: 20},
]}});

test('使用液态量求和，不把总降水或概率当雨量；窗口包含前一小时', () => {
    const result = summarize(sample(), now);
    assert.equal(result.amount, '时段雨量预报：10 mm');
    assert.equal(result.intensity, '最强小时平均雨强：强（8 mm/h）');
    assert.equal(result.probability, '最高降雨概率：90%（不是雨量）');
    assert.equal(result.window, '2026-10-06 09:00 — 2026-10-06 11:00（北京时间，2 小时）');
    assert.match(result.source, /Open-Meteo.*预报/);
});

test('弱中强界线采用小时量，不冒用24小时等级', () => {
    for (const [amount, expected] of [[0,'未预报到降雨'],[2.54,'弱'],[2.55,'中'],[7.62,'中'],[7.63,'强']]) {
        const payload = sample(); payload.data.timeline.forEach(item => {item.rainfall_mm = amount;});
        assert.equal(summarize(payload, now).rows[0].intensity, expected);
    }
});

test('任一小时雨量缺失则合计和最强级未知，但概率独立可用', () => {
    const payload = sample(); payload.data.timeline[0].rainfall_mm = null;
    const result = summarize(payload, now);
    assert.equal(result.amount, '时段雨量预报：未知');
    assert.equal(result.intensity, '小时雨强：未知');
    assert.match(result.probability, /90%/);
    assert.equal(result.rows[0].amount, '未知');
    assert.equal(result.rows[1].amount, '8 mm');
});

test('概率缺失不抹去雨量，也不使用概率推导雨强', () => {
    const payload = sample(); payload.data.timeline[1].precipitation_probability = null;
    const result = summarize(payload, now);
    assert.equal(result.probability, '最高降雨概率：未知');
    assert.equal(result.amount, '时段雨量预报：10 mm');
    assert.match(result.intensity, /强/);
});

test('缺单位或累计时段、错误时区均不得标注雨量等级', () => {
    for (const [key, value] of [['precipitation_unit','inch'],['rainfall_interval_minutes',null],['rainfall_time_reference','instant'],['timezone',null]]) {
        const payload = sample(); payload.data[key] = value;
        assert.equal(summarize(payload, now).amount, '时段雨量预报：未知');
        assert.equal(summarize(payload, now).rows.length, 0);
    }
});

test('时间缺口、乱序、重复或伪造日期不累计', () => {
    for (const time of ['2026-10-06T12:00:00+08:00','2026-10-06T09:00:00+08:00','2026-10-06T10:00:00+08:00','2026-02-30T10:00:00+08:00','not-time']) {
        const payload = sample(); payload.data.timeline[1].time = time;
        assert.equal(summarize(payload, now).rows.length, 0);
    }
});

test('负数、非有限数、布尔及字符串量不算真实数值', () => {
    for (const value of [-1,NaN,Infinity,true,'3']) {
        const payload = sample(); payload.data.timeline[0].rainfall_mm = value;
        assert.equal(summarize(payload, now).amount, '时段雨量预报：未知');
    }
});

test('无结果、模拟或无来源时保留未知，过期窗口明确失效', () => {
    assert.equal(summarize(null, now).amount, '时段雨量预报：未知');
    for (const [key, value] of [['available',false],['is_mock',true],['source','']]) {
        const payload = sample(); payload.data[key] = value;
        assert.equal(summarize(payload, now).rows.length, 0);
    }
    assert.match(summarize(sample(), Date.parse('2026-10-06T11:00:00+08:00')).window, /已过期/);
});

test('跨日与UTC时间按北京时间显示绝对窗口', () => {
    const payload = sample(); payload.data.timeline[0].time = '2026-10-06T16:00:00Z'; payload.data.timeline[1].time = '2026-10-06T17:00:00Z';
    assert.equal(summarize(payload, now).window, '2026-10-06 23:00 — 2026-10-07 01:00（北京时间，2 小时）');
});

test('标准ISO毫秒时间可正确解析', () => {
    const payload = sample(); payload.data.timeline.forEach(item => {item.time = new Date(item.time).toISOString();});
    assert.equal(summarize(payload, now).amount, '时段雨量预报：10 mm');
});
