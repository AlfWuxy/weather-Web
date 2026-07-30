import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import worker, {
  CONTENT_SIGNAL,
  DISCOVERY_RESOURCES,
  EDGE_VERSION,
  PUBLIC_URLS,
  handleRequest,
} from "../src/index.mjs";

const BASE_URL = "https://yilaoweather.org";

function request(path, options = {}) {
  return new Request(`${BASE_URL}${path}`, options);
}

async function originStub(incoming) {
  const url = new URL(incoming.url);
  return new Response(`origin:${url.hostname}${url.pathname}`, {
    status: 209,
  });
}

test("robots 允许公开抓取并保留私密路径与内容用途边界", async () => {
  const response = await handleRequest(
    request("/robots.txt?source=agent"),
    originStub,
  );
  const body = await response.text();

  assert.equal(response.status, 200);
  assert.equal(response.headers.get("content-signal"), CONTENT_SIGNAL);
  assert.equal(
    response.headers.get("x-yilao-edge-discovery"),
    EDGE_VERSION,
  );
  assert.equal(
    response.headers.get("cloudflare-cdn-cache-control"),
    null,
  );
  assert.match(body, /^User-agent: \*/);
  assert.match(body, /Content-Signal: ai-train=no, search=yes, ai-input=yes/);
  assert.match(body, /Allow: \/\n/);
  assert.match(body, /Allow: \/llms\.txt/);
  assert.match(body, /Disallow: \/admin/);
  assert.match(body, /Disallow: \/mp\/api\//);
  assert.match(body, /Disallow: \/healthz/);
  assert.match(body, /Disallow: \/account-link/);
  assert.match(
    body,
    /Sitemap: https:\/\/yilaoweather\.org\/sitemap\.xml/,
  );
});

test("sitemap 只列出五个当前在线匿名页面", async () => {
  const response = await worker.fetch(request("/sitemap.xml"));
  const body = await response.text();
  const locations = Array.from(
    body.matchAll(/<loc>([^<]+)<\/loc>/g),
    (match) => match[1],
  );

  assert.equal(response.status, 200);
  assert.equal(
    response.headers.get("content-type"),
    "application/xml; charset=utf-8",
  );
  assert.deepEqual(locations, PUBLIC_URLS);
  assert.equal(new Set(locations).size, 5);
  assert.ok(locations.every((url) => !url.includes("?")));
  assert.ok(
    !locations.includes(
      "https://yilaoweather.org/duchang-heat-vulnerability-map",
    ),
  );
});

test("llms 摘要只列公开页面并明确隐私边界", async () => {
  const response = await handleRequest(request("/llms.txt"), originStub);
  const body = await response.text();

  assert.equal(response.status, 200);
  assert.equal(response.headers.get("x-robots-tag"), "index, follow");
  for (const url of PUBLIC_URLS) {
    assert.ok(body.includes(url));
  }
  for (const privateTerm of [
    "登录后页面",
    "管理后台",
    "家庭与照护关系",
    "手机号",
    "微信身份",
    "绑定码",
    "用户精确位置",
  ]) {
    assert.ok(body.includes(privateTerm));
  }
  assert.ok(!body.includes("https://yilaoweather.org/admin"));
  assert.ok(!body.includes("https://yilaoweather.org/community-risk"));
  assert.ok(
    !body.includes(
      "https://yilaoweather.org/duchang-heat-vulnerability-map",
    ),
  );
});

test("HEAD 复用 GET 响应头且不返回正文", async () => {
  const response = await handleRequest(
    request("/robots.txt", { method: "HEAD" }),
    originStub,
  );

  assert.equal(response.status, 200);
  assert.equal(await response.text(), "");
  assert.equal(
    response.headers.get("content-type"),
    DISCOVERY_RESOURCES["/robots.txt"].contentType,
  );
});

test("发现文件拒绝写方法", async () => {
  const response = await handleRequest(
    request("/llms.txt", { method: "POST" }),
    originStub,
  );

  assert.equal(response.status, 405);
  assert.equal(response.headers.get("allow"), "GET, HEAD");
  assert.equal(response.headers.get("cache-control"), "no-store");
});

test("相似后缀和其他主机必须回源", async () => {
  const suffix = await handleRequest(
    request("/robots.txt.bak"),
    originStub,
  );
  const otherHost = await handleRequest(
    new Request("https://example.org/robots.txt"),
    originStub,
  );

  assert.equal(suffix.status, 209);
  assert.equal(
    await suffix.text(),
    "origin:yilaoweather.org/robots.txt.bak",
  );
  assert.equal(otherHost.status, 209);
  assert.equal(
    await otherHost.text(),
    "origin:example.org/robots.txt",
  );
});

test("Wrangler 只声明 apex 与 www 的六条发现文件路由", async () => {
  const configPath = new URL("../wrangler.jsonc", import.meta.url);
  const config = JSON.parse(await readFile(configPath, "utf8"));
  const patterns = config.routes.map((route) => route.pattern);

  assert.equal(config.name, "yilao-discovery-edge-v111");
  assert.equal(config.main, "src/index.mjs");
  assert.equal(config.workers_dev, false);
  assert.equal(patterns.length, 6);
  assert.deepEqual(
    new Set(patterns),
    new Set([
      "https://yilaoweather.org/robots.txt*",
      "https://yilaoweather.org/sitemap.xml*",
      "https://yilaoweather.org/llms.txt*",
      "https://www.yilaoweather.org/robots.txt*",
      "https://www.yilaoweather.org/sitemap.xml*",
      "https://www.yilaoweather.org/llms.txt*",
    ]),
  );
  assert.ok(
    config.routes.every(
      (route) => route.zone_name === "yilaoweather.org",
    ),
  );
});
