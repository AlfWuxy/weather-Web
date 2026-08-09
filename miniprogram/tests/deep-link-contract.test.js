const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const miniRoot = path.resolve(__dirname, '..');

function readPage(name, suffix) {
  return fs.readFileSync(path.join(miniRoot, 'pages', name, `index.${suffix}`), 'utf8');
}

test('缺少家人参数的私密深链停留在安全错误态且不跳预报', () => {
  const pages = ['template', 'action-checkin', 'diary', 'medications'];
  pages.forEach((name) => {
    const script = readPage(name, 'js');
    const view = readPage(name, 'wxml');
    assert.match(script, /Number\(options\.pair_id \|\| 0\) \|\| null/);
    assert.match(script, /缺少家人信息/);
    assert.match(view, /loadError/);
    assert.match(view, /返回|重新选择/);
    assert.doesNotMatch(`${script}\n${view}`, /pages\/forecast\/index/);
  });
});

test('不存在的家人标识关闭写入口并保留家庭照护返回路径', () => {
  const pages = ['elder-edit', 'template', 'action-checkin', 'diary', 'medications'];
  pages.forEach((name) => {
    const script = readPage(name, 'js');
    const view = readPage(name, 'wxml');
    assert.match(script, /(?:not_found|elder_not_found)/);
    assert.match(script, /contextReady:\s*false/);
    assert.match(view, /返回|家庭照护|重新选择/);
    assert.doesNotMatch(`${script}\n${view}`, /pages\/forecast\/index/);
  });
});

test('筛查深链找不到指定家人时只回退到已授权家人', () => {
  const script = readPage('health-assessment', 'js');
  const view = readPage('health-assessment', 'wxml');
  assert.match(script, /findIndex\(\(item\) => Number\(item\.pair_id\) === requestedPairId\)/);
  assert.match(script, /if \(elderIndex < 0\) elderIndex = 0/);
  assert.match(view, /返回家庭照护|重新选择/);
  assert.doesNotMatch(`${script}\n${view}`, /pages\/forecast\/index/);
});

test('身份页、退出与会话错误路径只允许登录、设置或公共首页', () => {
  const sources = [
    readPage('account', 'js'),
    readPage('settings', 'js'),
    readPage('bind-token', 'js'),
    fs.readFileSync(path.join(miniRoot, 'pages/elders/care-session.js'), 'utf8'),
  ].join('\n');
  assert.match(sources, /pages\/bind-token\/index/);
  assert.match(sources, /pages\/settings\/index/);
  assert.match(sources, /pages\/home\/index/);
  assert.doesNotMatch(sources, /pages\/forecast\/index/);
});
