# 宜老农业站内工作台

农业模块复用本站登录、导航和页脚，入口为 `/agriculture/`，接口前缀为 `/api/v1/agriculture/workbench`。运行源码位于 `vendor/yilao_agriculture`，无前端构建步骤；页面从同源子路径加载样式和 JavaScript，不使用 iframe。此接入默认关闭。

这是基于仓库当前主线的宿主集成。代码检查、合成演示和临时数据库测试不代表已部署、社区实际使用或健康效果验证。农业安排是依据录入条件生成的草稿；来源、个人限制、现场情况和实际完成情况仍需本人或协助者确认。

## 配置与启用

| 配置项 | 要求 |
| --- | --- |
| `YILAO_AGRICULTURE_WORKBENCH_ENABLED` | 默认 `0`；准备好独立存储后显式设为 `1` |
| `YILAO_AGRICULTURE_SOURCE_ROOT` | 默认仓库内 `vendor/yilao_agriculture`；如覆盖，须为无符号链接的绝对路径 |
| `YILAO_AGRICULTURE_ACCOUNT_DB` | 新的私有 SQLite 文件绝对路径；不能使用本站健康数据库 |
| `YILAO_AGRICULTURE_ARCHIVE_ROOT` | 私有天气原始资料目录绝对路径；不能放在公开静态目录 |
| `YILAO_AGRICULTURE_SITE_ORIGIN` | 用户实际访问的同源地址（协议和主机，必要时含端口）；反向代理须使请求来源与其一致 |

维护者须先选择农业专用私有存储位置。账户库和任务库只通过后台工具显式初始化，网页工厂不会自动建库或迁移本站数据。任务库位于账户库旁，文件名附加 `.weather-jobs.sqlite3`。示例操作使用维护者事先配置的环境文件，不包含实际部署路径：

启用前还须确认原站既有 `SECRET_KEY` 至少 32 字节，这是实测材料导出时派生账户标识的要求。只检查是否满足条件，不打印密钥，也不要为本次接入自动轮换原站密钥；若不满足，先保留关闭状态，由维护者评估原站会话及历史标识的影响。

```sh
PYTHONPATH=vendor/yilao_agriculture python -B -m integration.weather_site_worker --initialize
```

应先备份既有数据并分别管理本站数据库、农业账户库和天气归档的恢复策略。上传受农业模块 4 MB 上限和宿主已有 `MAX_CONTENT_LENGTH` 中更小者限制；代理还可以有自己的请求上限，本模块不放宽它们。

## 账户与数据边界

只有正式登录账户可使用；访客不能读写农业资料。账户主体来自服务端登录模型，真实资料与演示资料分别存储。所有账户接口要求页面注入的 `X-Yilao-Account-Context`，写请求还要求本站的 `X-CSRF-Token`。切换账户后，旧页面须刷新才能继续，不能通过参数指定其他账户。

农业页面对宿主内联脚本和样式定向附加 CSP nonce，其余页面保持原有处理。退出仍使用主站现有 POST 表单。本站已有认证、代理和安全配置仍由宿主管理，此接入不替代它们。

全量备份下载和“整理实测材料”是不同功能。后者去除姓名、坐标和自由文字，但仍是待复核材料，不能直接当作有效验证样本。账户恢复可能替换其他资料，应先导出留底；不能假定服务端保留自动历史备份。

## 天气来源

主动刷新天气返回202和账户专属任务编号；网页每2秒查询，最多观察120秒。达到观察期限或查询断线后可继续查看同一任务，不自动重新提交。后台取得固定单次模型预报，并通过本站现有 QWeather 认证和额度预占回调查询预警。网页不取数，不放宽原站禁止HTTP请求联网的门禁。未配置、预算不足或请求失败会保持未知状态，不视为无预警；模型起报、资料取得与预报签发时间分别处理。来源声明、导入文件和内容哈希不构成独立来源认证。

维护者须另外管理一个专用工作进程，使用与本站相同的正式Python环境、配置来源、服务身份及明确源码路径：

```sh
PYTHONPATH=vendor/yilao_agriculture python -B -m integration.weather_site_worker --once
PYTHONPATH=vendor/yilao_agriculture python -B -m integration.weather_site_worker --serve
```

`--once`处理至多一项，输出“已处理”不等于任务成功，应读取任务记录。部署时先检查队列可读和工作进程配置，再由宿主服务管理器持续运行；不要从HTTP请求启动线程调用工作进程。单项处理90秒、租约300秒，超时不自动重试；服务停机宽限至少120秒。归档及各账户子目录在取数前收紧至0700。

保存前重新核对账户创建代次、资料版本和租约。带注销墓碑的新版宿主必须提供原站 `owner_write_guard` 及可用的 `DISPATCH_LOCK_PATH`，以同一注销锁保护最终CAS；缺失时拒绝启动。旧版无墓碑模型使用原站数据库行锁保护最终账户核对与保存。账户库和任务库的跨库提交可能出现结果无法确认，此时必须先重新读取资料，不能自动重试取数或覆盖。

## 针对性检查

在仓库 Python 测试环境运行：

```sh
python -m pytest -q -W error::DeprecationWarning -W ignore::DeprecationWarning:flask_login.login_manager tests/test_agriculture_workbench.py tests/test_nav_offcanvas.py
```

农业专项调用真实 `create_app()`、用户模型、密码登录和 CSRF，使用新建临时数据库并阻止外部连接及既有数据库读取；包含账户切换保护、默认关闭、演示排程及独立复算、网页入队与后台取数、最终账户写入守卫。测试资料均为合成资料，不计为真实社区样本。Redis、微信、代理、真实供应商认证和浏览器操作仍须在相应发布环境单独验收。
