const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const script = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'js', 'risk-reminder.js'),
  'utf8',
);

function setup(copyText) {
  const listeners = {};
  const timers = new Map();
  let timerId = 0;
  const label = { textContent: '复制今日提醒' };
  const button = {
    addEventListener(type, handler) {
      this.listeners = this.listeners || {};
      this.listeners[type] = handler;
    },
    querySelector() {
      return label;
    },
  };
  const elements = {
    copyFamilyReminder: button,
    familyReminderMessage: { textContent: '记得喝水。' },
    familyReminderQuestion: { textContent: '现在方便吗？' },
    familyReminderCopyStatus: { textContent: '' },
  };
  const fakeDocument = {
    addEventListener(type, handler) {
      listeners[type] = handler;
    },
    getElementById(id) {
      return elements[id] || null;
    },
  };
  const fakeWindow = {
    CWClipboard: { copyText },
    clearTimeout(id) {
      timers.delete(id);
    },
    setTimeout(handler) {
      timerId += 1;
      timers.set(timerId, handler);
      return timerId;
    },
  };

  vm.runInNewContext(script, { document: fakeDocument, window: fakeWindow });
  listeners.DOMContentLoaded();
  return { button, elements, label, timers };
}

test('复制成功后按钮和读屏状态都会按时复位', async () => {
  const harness = setup(async () => true);

  await harness.button.listeners.click();
  assert.equal(harness.label.textContent, '已复制');
  assert.equal(
    harness.elements.familyReminderCopyStatus.textContent,
    '今日提醒已复制到剪贴板。',
  );

  Array.from(harness.timers.values()).at(-1)();
  assert.equal(harness.label.textContent, '复制今日提醒');
  assert.equal(harness.elements.familyReminderCopyStatus.textContent, '');
});

test('连续复制会撤销旧计时器，失败状态也会复位', async () => {
  let call = 0;
  const harness = setup(async () => {
    call += 1;
    return call === 1;
  });

  await harness.button.listeners.click();
  assert.equal(harness.timers.size, 1);
  await harness.button.listeners.click();
  assert.equal(harness.timers.size, 1);
  assert.equal(harness.label.textContent, '复制失败，请手动选择文字');
  assert.equal(
    harness.elements.familyReminderCopyStatus.textContent,
    '复制失败，请手动选择提醒文字。',
  );

  Array.from(harness.timers.values()).at(-1)();
  assert.equal(harness.label.textContent, '复制今日提醒');
  assert.equal(harness.elements.familyReminderCopyStatus.textContent, '');
});
