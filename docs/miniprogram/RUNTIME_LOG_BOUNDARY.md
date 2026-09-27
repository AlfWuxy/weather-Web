# 1.1.0 正式运行日志边界

核验日期：2026-07-21

适用范围：`yilaoweather.org` 正式站点、`case-weather.service` 和由该应用直接写出的 Python 日志。本文件不把产品事件表、数据库业务记录或部署事务副本混称为运行日志。

## 可保留字段

正式微信运行态的结构化请求日志只允许以下字段：

- 服务端随机生成的请求编号
- 请求方法
- 经过行动链接和追踪凭据替换后的路径
- Flask 接口名
- HTTP 状态码
- 请求耗时
- 受限的外部接口名称、状态和耗时

其他 Python 应用日志只保留 `python_log` 事件类型、日志模块、级别、函数和代码行。这些字段可以定位失败模块和对应源代码，再结合请求编号、状态和耗时排查问题。

正式运行态会在任何 Python handler 写出前统一移除原始 `msg`、格式化参数、异常正文、 traceback 和 stack。测试使用独立 sentinel 证明以下内容不会进入输出：

- 请求或响应正文
- 查询参数
- IP 与转发链
- User-Agent
- 请求头
- SQL 参数
- 会话 token
- 用户坐标

正式微信运行态还必须关闭所有第三方错误监控和性能追踪：`SENTRY_DSN` 必须为空，`SENTRY_TRACES_SAMPLE_RATE` 与 `SENTRY_SEND_PII` 必须显式为 `0`。发布表单、候选环境或应用启动配置中任一项不满足时立即停止，禁止把应用日志、异常上下文、请求信息或用户资料发送到第三方监控服务。

## Nginx 边界

目标站点是本机 Nginx 的默认站点。站点 `server` 直接层级必须同时且唯一声明：

```nginx
access_log off;
error_log /dev/null crit;
```

`access_log off` 阻止站点访问记录。把站点请求级 `error_log` 丢弃到 `/dev/null`，用于避免异常请求把客户端 IP 写入 Nginx 错误明细。Nginx 主进程的全局日志继续保留启动和配置错误，站点排障依靠应用结构化日志。部署器会在创建新虚拟环境和激活候选版本前运行：

```bash
python3 scripts/verify_runtime_log_boundary.py --active-nginx
```

校验器先由 `/usr/sbin/nginx -T` 完成语法检查并读取完整活动配置，只接受目标 `server` 直接层级的唯一声明。注释、其他站点、`include` 和 `location` 子块不能满足或覆盖门禁。

## systemd journal 实际轮转边界

2026-07-21 的只读核验结果：

- 服务器运行 systemd 252。
- `case-weather.service` 使用 `StandardOutput=journal`，`StandardError=inherit`。
- journald 主配置和 drop-in 没有对 `Storage`、`SystemMaxUse`、`SystemKeepFree`、`MaxRetentionSec` 或 `MaxFileSec` 设置显式覆盖。
- `MaxRetentionSec=0`，没有按固定天数删除全部日志的策略。
- `MaxFileSec` 默认 1 个月，只控制单个 journal 文件何时轮转，不代表一个月后删除全部日志。
- `SystemMaxUse` 默认目标为日志所在文件系统容量的 10%，且上限 4 GiB。
- `SystemKeepFree` 默认要求保留文件系统容量的 15%，且该保留值上限 4 GiB。
- 核验时 archived 与 active journal 合计约 2.9 GiB。
- 容量回收只删除归档文件；活动文件可能使实际占用短时高于目标。

因此，对外准确表述是：“没有固定保存天数，按磁盘容量边界和日志量自动轮转覆盖。”不能表述为固定 30 天或固定 1 个月删除。

## 发布核验

每次正式发布都要完成以下步骤：

1. 运行日志边界单元测试与敏感 sentinel 测试。
2. 确认正式表单和候选运行环境保持 `SENTRY_DSN` 为空、`SENTRY_TRACES_SAMPLE_RATE=0`、`SENTRY_SEND_PII=0`。
3. 运行 `nginx -t`。
4. 运行站点配置门禁。
5. reload Nginx 后验证目标站点仍包含两项唯一声明。
6. 只输出计数的方式核对切换后的验证窗口，确认应用 journal 不含正文、IP、User-Agent、请求头、SQL 参数或外部请求编号 sentinel。
7. 记录 `journalctl --disk-usage` 和 journald 配置元数据，不读取或复制用户日志正文到发布证据。

任何一项失败都阻断 1.1.0 正式激活、重新上传和审核提交。
