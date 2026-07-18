# 天气 API 额度与 30 分钟缓存

## 目标

- 天气来源固定为都昌县 `116.20,29.27`。
- 乡镇、社区、老人档案和页面筛选共享县级快照。
- 小程序访问量增加时，QWeather 调用量不随用户数线性增长。
- 开发、自动化测试和视觉检查的真实 QWeather 调用数为 0。

## 数据链路

```text
部署或开机后 bootstrap timer 完整等待 30 分钟
        ↓
首次都昌同步完成后启动 recurring timer
        ↓
服务器单次都昌同步周期
        ↓
部署期网络闸门 + 预算预占与 fail-closed 检查
        ↓
Redis / 数据库持久化当前天气、七日预报、预警、小时降水与 MiniProgramSnapshot
        ↓
小程序 bootstrap 与普通 Web 天气接口只读缓存
        ↓
小程序本地 30 分钟缓存 + 并发去重
        ↓
所有页面共享同一 snapshot_id
```

## 服务端规则

1. `case-weather-cache-bootstrap.timer` 在部署或开机后完整等待 30 分钟，再拉起首次同步；首次尝试结束后才启动 `case-weather-cache.timer`，此后每 30 分钟触发一次。
2. 默认预热列表只能包含都昌县。
3. 普通 HTTP 请求不能触发上游 QWeather 刷新；即使调用方误传联网参数，请求上下文也必须强制只读，预算预占必须零计数拒绝。
4. 每次上游请求前必须先通过 `QWEATHER_NETWORK_NOT_BEFORE_EPOCH` 网络闸门，再通过月度预算预占；闸门阻断不得增加 Redis 或本地预算计数。
5. Redis 已配置且不可用时，`QWEATHER_BUDGET_FAIL_CLOSED=1` 会阻断请求。
6. 快照保留抓取时间、过期时间、来源、缺失字段和 stale 状态。
7. 无真实数据时返回“正在更新”，禁止用 mock 数据生成生产风险结论。
8. 离线读取旧数据库缓存时必须继承原始 `fetched_at`，禁止把旧天气重新包装成新鲜快照。
9. 官方预警必须区分“成功返回空列表”和“配置、额度、认证、网络或解析失败”。
10. 社区风险预计算只能读取现有真实天气缓存，缓存缺失或仅有 mock 数据时跳过，禁止自行访问上游。
11. 不可变 release 上传必须排除所有 `.env*` 和 `project.private.config.json`。
12. 小程序快照与 Web 七日预报共享同一个 QWeather 周期；Web 小时降水时间轴由该周期额外写入 24 小时 Open-Meteo 缓存，页面访问不得临时抓取。
13. 激活事务只有在服务、timer、`OnSuccess`、30 分钟剩余窗口、`current` 链接、暂存环境清理和公网健康检查全部通过后才能写入 `COMMITTED`。
14. Web 实况与小时降水超过 30 分钟后必须返回“更新中”，禁止继续生成健康风险或展示已过期小时线；小程序可保留旧快照，但必须明确标记 stale 和原始更新时间。
15. 七日预报缓存必须从都昌县本地今天开始连续 7 天、关键数值完整、逐项来源为 QWeather，且数据库记录未标记为 mock；任何条件不满足时整组拒绝写入和复用。
16. 小时降水缓存只接受 Open-Meteo 的非 mock 结果；1 至 24 条时间必须有效、递增且不重复，概率、降水量、温度和风险等级必须在可信范围内。
17. 正式天气凭据必须为本项目独立使用。发布当天人工读取一次 QWeather 控制台北京时间当月已用量，并以只增不减方式写入 Redis 月计数基线。
18. 发布门禁按最坏情况预留整月剩余用量：`ceil(距北京时间下月起点秒数 / 1800) × 3 + 3`。前一项对应每 30 分钟的实况、七日预报和预警，最后 3 次对应唯一正式烟测；基线与预留之和超过月上限时停止发布。
19. 正式预算 Redis 必须启用 AOF，`appendfsync` 只接受 `everysec` 或 `always`。PING、持久化配置、加载状态、最近写入状态或探测权限任一异常时 fail-closed。

## 客户端规则

1. storage key 必须包含 schema 版本。
2. 缓存 TTL 固定为 1,800 秒。
3. 同一时刻只允许一个 bootstrap 请求，其余调用复用同一个 Promise。
4. 网络失败时可显示上一份快照，并明确标注“数据已过期”。
5. 过期快照请求失败后启用 60 秒退避，避免弱网页面切换反复请求。
6. 下拉刷新在 30 分钟内仍复用快照。
7. 退出账号只清除身份和健康数据缓存，公共天气快照可以继续复用。
8. 客户端不得包含 QWeather host、key 或直接请求逻辑。

## 回归门槛

- 29 分 59 秒仍命中客户端和服务端缓存。
- 30 分 01 秒标记过期，下一次有效同步才能替换 snapshot_id。
- 10 个老人、20 个页面并发读取只出现 1 个 bootstrap 请求。
- 所有社区名称都映射到同一都昌快照。
- 测试进程拦截 QWeather host，出现访问即失败。
- 当前天气、七日预报和小时降水 HTTP 路由在空缓存或过期缓存下均不会调用 fetcher；过期实况和小时线不会继续参与展示或风险计算。
- 月预算为 0 时，系统可启动、页面可展示缓存状态、QWeather 请求数为 0。
- 部署完成后 bootstrap timer 为 active，recurring timer 为 inactive 且 disabled；30 分钟窗口内预算计数保持不变。
- 网络闸门值无效时 fail-closed，过期后无需清变量或重启即可自动放行。
- 原子激活完整状态复核失败时保留新数据库和 release，写入 `POST_COMMIT_ATTENTION.txt`，且下一次激活被阻断。

## 单次真实联调

真实联调前先读取预算快照，确认没有正在运行的手工诊断。一次性开关只在该命令进程内生效，完成后立即关闭。保存脱敏响应、snapshot_id、时间戳和预算计数差值，禁止保存认证头或 key。

正式发布由外置状态目录保存耐久 receipt，目录名绑定冻结 commit 与天气语义配置 SHA-256。天气指纹只纳入会改变 QWeather HTTP 请求、预算或正式快照判定的字段，包括认证模式与凭据、API Base、canonical location、预算门禁、缓存 TTL、同步位置和天气不可用策略。AppID、AppSecret、隐私版本、WxPusher、GIS 开关和公开域名不参与天气指纹；轮换这些字段仍复用同一个 receipt。QWeather key 或其他天气配置变化会形成新的天气指纹。激活事务会在开放 QWeather 网络闸门前创建 `started`，通过 QWeather 官方实况、七日预报、预警状态和快照新鲜度校验后原子写入 `completed`。相同绑定的 completed 只复用仍然新鲜的 snapshot_id；started 未完成、receipt 损坏、快照丢失或过期都会 fail-closed，并要求人工核对上游计数。自动流程不会删除 receipt 或再次发起天气同步。

正式烟测固定向 `weather_cache_sync.sh` 传入 `--skip-nowcast`，因此不会请求或写入短时 nowcast。该烟测预计最多调用 QWeather 实况、七日预报和预警三个 endpoint；预报或预警命中新鲜缓存时调用数会更少。每 30 分钟的常规定时同步不传该参数，继续维护短时 nowcast 缓存。

Open-Meteo、fallback、mock 和 demo 数据不能完成正式烟测。动态的 `QWEATHER_NETWORK_NOT_BEFORE_EPOCH` 不参与天气语义配置指纹，避免同一发布仅因 30 分钟闸门时间变化而绕过单次约束。

如果要求严格只有 1 次 QWeather HTTP 请求，应只选定一个 endpoint 验证。其他实况、空气质量和预警字段显示暂不可用，不能追加第二次请求。
