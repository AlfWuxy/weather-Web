'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const pageDir = path.join(__dirname, '..', 'miniprogram', 'pages', 'bind-token');
const js = fs.readFileSync(path.join(pageDir, 'index.js'), 'utf8');
const wxml = fs.readFileSync(path.join(pageDir, 'index.wxml'), 'utf8');

function blockAfter(source, signature) {
  const start = source.indexOf(signature);
  assert.notEqual(start, -1, signature);
  const open = source.indexOf('{', start);
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === '{') depth += 1;
    else if (source[i] === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(open + 1, i);
    }
  }
  assert.fail(`unterminated ${signature}`);
}

test('bind-token 输入框默认密码遮罩，等价于 type=password', () => {
  const inputs = wxml.match(/<input\b[\s\S]*?\/>/g) || [];
  assert.equal(inputs.length, 1);
  const input = inputs[0];
  assert.match(input, /\bpassword\s*=\s*"\{\{\s*!showToken\s*\}\}"/);
  assert.match(input, /\bvalue\s*=\s*"\{\{\s*tokenInput\s*\}\}"/);
  assert.doesNotMatch(input, /\btype\s*=\s*"(?:text|search|email)"/);
  assert.match(js, /showToken:\s*false/);
  assert.match(js, /tokenInput:\s*''/);
});

test('显示/隐藏只翻转遮罩，不把已保存凭证写回输入框', () => {
  assert.match(wxml, /bindtap="onToggleToken"/);
  assert.match(wxml, /\{\{\s*showToken\s*\?\s*'隐藏'\s*:\s*'显示'\s*\}\}/);
  const toggle = blockAfter(js, 'onToggleToken()');
  assert.match(toggle, /showToken:\s*!this\.data\.showToken/);
  assert.doesNotMatch(toggle, /api_token|tokenInput/);
  const hint = blockAfter(js, 'function bindingHint');
  assert.match(hint, /tokenTail:\s*value\.length > 4 \? value\.slice\(-4\)/);
  assert.doesNotMatch(hint, /tokenInput/);
  assert.match(wxml, /末尾 4 位 \{\{tokenTail\}\}/);
  assert.doesNotMatch(wxml, /\{\{[^}]*api_token[^}]*\}\}/);
});

test('isMaskedToken 拒绝界面掩码，onBind 在落库前返回', () => {
  const body = blockAfter(js, 'function isMaskedToken');
  assert.match(body, /token\.includes\('已绑定'\)/);
  assert.match(body, /token\.includes\('末尾'\)/);
  assert.match(body, /\/\^\\\*\+\$\/\.test\(token\)/);
  const isMaskedToken = new Function('token', body);

  const hints = [...wxml.matchAll(/>([^<]*已绑定[^<]*)</g)].map((match) => (
    match[1].replace(/\{\{[^}]+\}\}/g, '').trim()
  ));
  assert.ok(hints.length >= 2);
  for (const masked of ['****', '*', '**********', '已绑定', '末尾 4 位', ...hints]) {
    assert.equal(isMaskedToken(masked), true, masked);
  }
  for (const raw of ['', 'ab12cd34ef56', 'abc*', '***a', 'token-live-not-a-mask']) {
    assert.equal(isMaskedToken(raw), false, raw);
  }

  const onBind = blockAfter(js, 'onBind()');
  const maskAt = onBind.indexOf('isMaskedToken(token)');
  const storeAt = onBind.indexOf("setStorageSync('api_token'");
  assert.ok(maskAt > onBind.indexOf('.trim()'));
  assert.ok(storeAt > maskAt);
  assert.match(
    onBind,
    /if\s*\(\s*isMaskedToken\(token\)\s*\)\s*\{[\s\S]*?title:\s*'请重新输入新 Token'[\s\S]*?return;/
  );
});
