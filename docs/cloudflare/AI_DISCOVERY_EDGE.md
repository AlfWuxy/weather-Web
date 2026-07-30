# AI 发现入口临时边缘发布

## 用途

源站完整 v1.1.1 尚未发布时，Cloudflare Worker 临时提供：

- `/robots.txt`
- `/sitemap.xml`
- `/llms.txt`

Worker 不接管网页、登录、API、天气、风险或用户数据。源站发布完成并验证三个端点后，应删除 Worker Route，避免长期维护两份内容。

## 内容用途边界

公开内容声明：

```text
Content-Signal: ai-train=no, search=yes, ai-input=yes
```

这允许搜索索引和即时 AI 引用，不授权模型训练。登录后内容、管理后台、API、家庭关系、手机号、微信身份、绑定码和精确位置继续禁止抓取。

## 路由范围

生产仅使用以下六条 HTTPS Route：

```text
https://yilaoweather.org/robots.txt*
https://yilaoweather.org/sitemap.xml*
https://yilaoweather.org/llms.txt*
https://www.yilaoweather.org/robots.txt*
https://www.yilaoweather.org/sitemap.xml*
https://www.yilaoweather.org/llms.txt*
```

末尾通配符用于覆盖查询参数。Worker 内会再次严格校验 `URL.pathname`，因此 `/robots.txt.bak` 等相似路径仍然回源。

## 上线前门禁

1. Cloudflare AI Crawl Control 中目标 crawler 保持 Allow。
2. Managed robots.txt 保持关闭。
3. Zone 中不存在覆盖全站的 Worker Route。
4. 本地 Node 契约测试通过 GET、HEAD、405、相似后缀回源测试；`workers.dev` 保持关闭，避免重复公开入口。
5. GitHub 的 `Cloudflare 边缘可发布提交证明` 属于待发布精确提交并通过。

## 验收

先只绑定 apex `/robots.txt*` 作为金丝雀。该路径验证通过后，再按 apex、`www` 顺序增加其余五条 Route。每个正式 URL 都要验证：

- HTTP 200。
- `X-Yilao-Edge-Discovery: v1.1.1`。
- `Content-Signal: ai-train=no, search=yes, ai-input=yes`。
- Content-Type 与正文正确。
- `robots.txt` 含 Sitemap 和私密路径边界。
- Sitemap 只含六个固定匿名 URL。
- `llms.txt` 不含私密页面 URL。

还要确认 `/`、`/risk`、`/healthz`、`/admin` 和 `/mp/api/` 的状态、安全头与缓存行为没有变化。

## 回滚

按 `www`、apex 的逆序删除六条 Route。Worker 脚本可以保留为未绑定版本。随后精确清理这六个 URL 的 Cloudflare 缓存，并确认请求重新回到源站。

源站正式更新后，先核对源站三个端点的正文与响应头，再解绑 Worker Route并定点清缓存。
