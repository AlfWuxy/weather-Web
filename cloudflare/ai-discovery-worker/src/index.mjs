/**
 * 宜老天气通 AI 发现入口临时边缘层。
 *
 * 该 Worker 只接管 robots.txt、sitemap.xml 与 llms.txt。
 * 其他路径必须继续回源，避免影响网页、登录、API 和天气服务。
 */

export const EDGE_VERSION = "v1.1.1";
export const CONTENT_SIGNAL =
  "ai-train=no, search=yes, ai-input=yes";

export const PUBLIC_URLS = Object.freeze([
  "https://yilaoweather.org/",
  "https://yilaoweather.org/risk",
  "https://yilaoweather.org/cooling",
  "https://yilaoweather.org/duchang-heat-vulnerability-map",
  "https://yilaoweather.org/transparency",
  "https://yilaoweather.org/about/trust-network",
]);

const ROBOTS_BODY = `User-agent: *
Content-Signal: ${CONTENT_SIGNAL}
Allow: /
Allow: /llms.txt
Disallow: /admin
Disallow: /api/
Disallow: /mp/api/
Disallow: /dashboard
Disallow: /caregiver
Disallow: /community
Disallow: /community-risk
Disallow: /healthz
Disallow: /logout
Disallow: /profile
Disallow: /account-link
Disallow: /family-members
Disallow: /pairs
Disallow: /location
Disallow: /health-assessment
Disallow: /medication-reminders
Disallow: /health-diary
Disallow: /forecast-7day
Disallow: /ml-prediction
Disallow: /ai-qa
Disallow: /chronic-risk
Disallow: /annual-report
Disallow: /analysis/
Disallow: /alerts/
Disallow: /reports
Disallow: /guest
Disallow: /action
Disallow: /elder
Disallow: /e/
Disallow: /t/
Sitemap: https://yilaoweather.org/sitemap.xml
`;

const SITEMAP_BODY =
  '<?xml version="1.0" encoding="UTF-8"?>' +
  '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' +
  PUBLIC_URLS.map((url) => `<url><loc>${url}</loc></url>`).join("") +
  "</urlset>";

const LLMS_BODY = `# 宜老天气通

> 面向都昌县老人、家属和社区的天气风险行动服务。本文件是实验性 AI 发现摘要，不代表正式或通用的网络标准。

## Public pages

- [主页](https://yilaoweather.org/)
- [公开天气风险与行动建议](https://yilaoweather.org/risk)
- [已核验避暑资源](https://yilaoweather.org/cooling)
- [都昌县热暴露与老年人口脆弱性地图](https://yilaoweather.org/duchang-heat-vulnerability-map)
- [指标透明度](https://yilaoweather.org/transparency)
- [信任网络说明](https://yilaoweather.org/about/trust-network)

## Discovery

- [Sitemap](https://yilaoweather.org/sitemap.xml)
- [Robots policy](https://yilaoweather.org/robots.txt)

## Privacy boundary

- 只抓取上方公开、县域聚合或方法说明页面。
- 不抓取登录后页面、管理后台、API、家庭与照护关系、社区私密工作区、手机号、微信身份、绑定码或用户精确位置。
- 地表温度不是气温、体感温度或个人医疗风险评分；候选地点必须完成人工核验后才会进入正式资源页。

English note: Public research content is de-identified and aggregated within Duchang County. Private user and community data must not be crawled.
`;

export const DISCOVERY_RESOURCES = Object.freeze({
  "/robots.txt": Object.freeze({
    body: ROBOTS_BODY,
    contentType: "text/plain; charset=utf-8",
    maxAge: 300,
    robotsTag: null,
  }),
  "/sitemap.xml": Object.freeze({
    body: SITEMAP_BODY,
    contentType: "application/xml; charset=utf-8",
    maxAge: 3600,
    robotsTag: null,
  }),
  "/llms.txt": Object.freeze({
    body: LLMS_BODY,
    contentType: "text/plain; charset=utf-8",
    maxAge: 3600,
    robotsTag: "index, follow",
  }),
});

const ALLOWED_HOSTS = new Set([
  "yilaoweather.org",
  "www.yilaoweather.org",
]);

function discoveryHeaders(resource) {
  const cachePolicy = `public, max-age=${resource.maxAge}`;
  const headers = new Headers({
    "Cache-Control": cachePolicy,
    "Content-Language": "zh-CN",
    "Content-Signal": CONTENT_SIGNAL,
    "Content-Type": resource.contentType,
    "X-Content-Type-Options": "nosniff",
    "X-Yilao-Edge-Discovery": EDGE_VERSION,
  });
  if (resource.robotsTag) {
    headers.set("X-Robots-Tag", resource.robotsTag);
  }
  return headers;
}

export async function handleRequest(request, originFetch = fetch) {
  const url = new URL(request.url);
  const resource = DISCOVERY_RESOURCES[url.pathname];
  if (!ALLOWED_HOSTS.has(url.hostname.toLowerCase()) || !resource) {
    return originFetch(request);
  }

  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response("Method Not Allowed\n", {
      status: 405,
      headers: {
        Allow: "GET, HEAD",
        "Cache-Control": "no-store",
        "Content-Type": "text/plain; charset=utf-8",
        "X-Content-Type-Options": "nosniff",
        "X-Yilao-Edge-Discovery": EDGE_VERSION,
      },
    });
  }

  return new Response(
    request.method === "HEAD" ? null : resource.body,
    {
      status: 200,
      headers: discoveryHeaders(resource),
    },
  );
}

export default {
  fetch(request) {
    return handleRequest(request);
  },
};
