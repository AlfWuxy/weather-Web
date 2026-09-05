function parsePairId(raw) {
  if (raw === undefined || raw === null || raw === '') return null;
  const n = parseInt(raw, 10);
  if (!Number.isFinite(n) || n <= 0) return null;
  return n;
}

function toFiniteNumber(value) {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function formatTemp(value) {
  const n = toFiniteNumber(value);
  return n === null ? '-' : String(n);
}

function warningListKey(item, index) {
  const raw = item && item.raw;
  const rawId = raw && (raw.id || raw.warningId);
  if (rawId) return String(rawId);
  return ['w', (item && item.start_time) || '', (item && item.type) || '', (item && item.level) || '', (item && item.title) || '', String(index)].join('|');
}

function personalizedCareNotes(chronicDiseases) {
  const diseases = (chronicDiseases || []).filter(Boolean);
  const text = diseases.join('、');
  if (!text) return [];
  const notes = [`慢病提示（可选登记）：${text}`];
  const coldSensitive = diseases.some((d) => d.indexOf('呼吸') !== -1 || d.indexOf('慢阻肺') !== -1 || d.indexOf('支气管') !== -1);
  const heatSensitive = diseases.some((d) => d.indexOf('高血压') !== -1 || d.indexOf('冠心病') !== -1 || d.indexOf('脑卒中') !== -1);
  if (coldSensitive) {
    notes.push('寒冷时更要注意保暖、减少外出，预防感冒与呼吸道不适。');
  }
  if (heatSensitive) {
    notes.push('高温时注意补水、避免暴晒和剧烈活动，留意头晕胸闷等不适。');
  }
  return notes;
}

function buildMessage({
  trigger,
  elderName,
  relation,
  locationText,
  tmax,
  tmin,
  chronicDiseases,
  weatherAvailable,
}) {
  if (weatherAvailable === false) {
    const lines = [
      '【天气更新中】',
      '风险等级暂不显示。仍可打开行动页完成安全确认或求助。',
    ];
    const location = (locationText || '').trim();
    if (location) lines.push(`地点：${location}`);
    return lines.join('\n');
  }

  let address = '你';
  if (relation === '母亲' || relation === '妈妈' || relation === '妈') address = '妈';
  else if (relation === '父亲' || relation === '爸爸' || relation === '爸') address = '爸';
  else if (elderName) address = elderName;

  const tmaxN = toFiniteNumber(tmax);
  const tminN = toFiniteNumber(tmin);
  const tmaxS = tmaxN === null ? null : String(Math.round(tmaxN));
  const tminS = tminN === null ? null : String(Math.round(tminN));

  const lines = [];
  if (trigger === 'cold') {
    let line1 = `【寒潮提醒】${address}，我看到你那边今天可能比较冷`;
    if (tminS !== null) line1 += `（最低约 ${tminS}°C）`;
    line1 += '。';
    lines.push(line1);
    lines.push('建议：尽量少出门，外出注意保暖防滑；室内注意保暖，别受凉。');
  } else if (trigger === 'heat') {
    let line1 = `【高温提醒】${address}，我看到你那边今天可能会很热`;
    if (tmaxS !== null) line1 += `（最高约 ${tmaxS}°C）`;
    line1 += '。';
    lines.push(line1);
    lines.push('建议：避开中午外出，多喝水；室内开风扇/空调或找阴凉处休息。');
  } else {
    lines.push(`【日常提醒】${address}，我这边看看你那边天气有变化，注意劳逸结合，出门记得带水/外套。`);
  }

  lines.push(`地点：${locationText || '-'}`);
  personalizedCareNotes(chronicDiseases).forEach((line) => lines.push(line));
  lines.push('说明：这是行动提醒，不提供医疗诊断/治疗建议；如明显不适请及时就医。');
  return lines.join('\n');
}

function thresholdKind(tmax, tmin, weatherAvailable) {
  if (!weatherAvailable) return '';
  const maxN = toFiniteNumber(tmax);
  const minN = toFiniteNumber(tmin);
  if (maxN !== null && maxN >= 35) return 'heat';
  if (minN !== null && minN <= 5) return 'cold';
  return '';
}

module.exports = {
  parsePairId,
  toFiniteNumber,
  formatTemp,
  warningListKey,
  personalizedCareNotes,
  buildMessage,
  thresholdKind,
};
