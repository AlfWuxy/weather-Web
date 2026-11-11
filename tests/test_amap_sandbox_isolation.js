'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.join(__dirname, '..');

function htmlFiles(dir) {
  const found = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) found.push(...htmlFiles(full));
    else if (entry.name.endsWith('.html')) found.push(full);
  }
  return found;
}

function iframeTags(html) {
  return html.match(/<iframe\b[\s\S]*?>/gi) || [];
}

function sandboxTokens(tag) {
  const valued = tag.match(/\bsandbox\s*=\s*(?:"([^"]*)"|'([^']*)')/i);
  if (valued) return (valued[1] ?? valued[2]).trim().split(/\s+/).filter(Boolean);
  assert.match(tag, /\bsandbox(?:\s|>|\/)/i, `iframe 缺少 sandbox 属性: ${tag}`);
  return [];
}

test('父页用 iframe 加载 amap-sandbox，sandbox 仅为 allow-scripts 或更严', () => {
  const hits = [];
  for (const file of htmlFiles(path.join(root, 'templates'))) {
    const html = fs.readFileSync(file, 'utf8');
    for (const tag of iframeTags(html)) {
      if (/amap-sandbox\.html/.test(tag)) hits.push({ file, html, tag });
    }
  }

  assert.ok(hits.length >= 1, '父页必须用 iframe 加载 amap-sandbox.html');
  assert.ok(hits.some((hit) => hit.file.endsWith(path.join('templates', 'community_risk.html'))));

  for (const hit of hits) {
    const tokens = sandboxTokens(hit.tag);
    assert.equal(
      tokens.some((token) => token.toLowerCase() === 'allow-same-origin'),
      false,
      hit.tag
    );
    assert.deepEqual(
      tokens.filter((token) => token.toLowerCase() !== 'allow-scripts'),
      [],
      `sandbox 比 allow-scripts 更松: ${hit.tag}`
    );
    assert.doesNotMatch(hit.html, /allow-same-origin/);
    const id = hit.tag.match(/\bid\s*=\s*"([^"]+)"/);
    assert.ok(id, hit.tag);
    assert.match(hit.html, new RegExp(`getElementById\\(\\s*'${id[1]}'\\s*\\)`));
    assert.match(hit.tag, /amap-sandbox\.html/);
  }

  const sandboxHtml = fs.readFileSync(path.join(root, 'static', 'amap-sandbox.html'), 'utf8');
  assert.equal(iframeTags(sandboxHtml).some((tag) => /allow-same-origin/i.test(tag)), false);
  assert.doesNotMatch(sandboxHtml, /allow-same-origin/);
  assert.match(sandboxHtml, /window\.origin\s*!==\s*'null'/);
});
