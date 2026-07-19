const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const PACKAGE_BUDGET_BYTES = 1.5 * 1024 * 1024;
const repoRoot = path.resolve(__dirname, '../..');
const miniprogramRoot = path.join(repoRoot, 'miniprogram');

function normalizeRelative(value) {
  return String(value || '')
    .replace(/\\/g, '/')
    .replace(/^\.\//, '')
    .replace(/^\/+|\/+$/g, '');
}

function collectFiles(directory, result) {
  fs.readdirSync(directory, { withFileTypes: true }).forEach((entry) => {
    const absolutePath = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      collectFiles(absolutePath, result);
      return;
    }
    if (entry.isFile()) result.push(absolutePath);
  });
}

function entryMatches(entry, candidate) {
  const value = normalizeRelative(entry && entry.value);
  const relative = normalizeRelative(candidate);
  if (!value || !relative) return false;
  if (entry.type === 'folder') return relative === value || relative.startsWith(`${value}/`);
  return entry.type === 'file' && relative === value;
}

function ignoredByPackConfig(file, ignoreEntries) {
  const candidates = [
    path.relative(repoRoot, file),
    path.relative(miniprogramRoot, file),
  ];
  return ignoreEntries.some((entry) => candidates.some((candidate) => entryMatches(entry, candidate)));
}

test('生产小程序包保持低于 1.5 MiB，并排除测试与外部 GIS 数据', () => {
  const project = JSON.parse(fs.readFileSync(path.join(repoRoot, 'project.config.json'), 'utf8'));
  const ignoreEntries = project.packOptions && Array.isArray(project.packOptions.ignore)
    ? project.packOptions.ignore
    : [];

  assert.ok(
    ignoreEntries.some((entry) => entry.type === 'folder' && normalizeRelative(entry.value) === 'tests'),
    '工程配置必须按小程序根目录排除 tests'
  );
  assert.ok(
    ignoreEntries.some((entry) => entry.type === 'folder' && normalizeRelative(entry.value) === 'miniprogram/tests'),
    '工程配置必须按仓库根目录排除 miniprogram/tests'
  );

  const allFiles = [];
  collectFiles(miniprogramRoot, allFiles);
  const productionFiles = allFiles.filter((file) => !ignoredByPackConfig(file, ignoreEntries));
  const packagedGeojson = productionFiles.filter((file) => path.extname(file).toLowerCase() === '.geojson');
  const totalBytes = productionFiles.reduce((sum, file) => sum + fs.statSync(file).size, 0);

  assert.deepEqual(packagedGeojson, [], 'GIS GeoJSON 必须继续由后端按需下载，不得打进小程序包');
  assert.ok(
    totalBytes < PACKAGE_BUDGET_BYTES,
    `生产小程序包 ${totalBytes} B 超过内部预算 ${PACKAGE_BUDGET_BYTES} B`
  );
});
