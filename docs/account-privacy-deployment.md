# 生产账号恢复与隐私增量发布

## 迁移与数据保留

基于生产 c47f6ba 的原 0001–0034 链追加 `0035_integrity_accounts`，不携带旧候选 0011–0018 的分叉链。保留 `users.auth_version`、`deleted_at`、`health_sensitive_consent_version`、`health_sensitive_consented_at` 与历史账号记录；不把旧邮箱标为已验证。家庭成员增加同名独立同意回执，初始 NULL；家庭关系不自动授予本人健康信息处理或密码恢复权限。旧用户有效健康回执继续按生产 `current_privacy_version()` 检查；不存在回执的资料不可用。

0035 新建 recovery_delegates、account_email_tokens，并新增 users.email_verified_at。迁移支持模型 create_all 的预建结构，严格核对类型、空值、主键、唯一与外键；不兼容结构会停止。0036 由产品增量补 CoolingResource 场所核验字段及 CoolingFeedback，账号注销清理会引用这张表，必须发布完整迁移链。

发布前由部署操作者取得生产实际 Alembic 版本与 schema 摘要（不要输出账号/健康记录），使用现有备份机制做 SQLite 一致性备份；核对备份 `PRAGMA integrity_check` 为 ok、`PRAGMA foreign_key_check` 无行。在隔离副本运行完整生产增量迁移，比较用户/家庭主键与行数、密码摘要不变、auth_version不变、历史同意回执不变、旧邮箱未验证、成员新同意NULL。输出仅统计及断言结果，不输出个人值。

迁移入口沿用 `VENV_PY=<发布解释器> bash scripts/server_migrate.sh`；版本库会跳过 create_all，最后验证 current 等于唯一 head。勿 stamp 跨过生产已有迁移，勿使用旧候选 Alembic 目录。0035 downgrade 只回退版本标记、保留所有追加结构与数据；更低版本原有降级预检继续生效，不自动删除账号恢复/回执数据；失败回退应保留新字段/新表并回滚应用，若必须恢复数据库，先停写并由部署事务执行经验证备份恢复，避免丢失发布后的用户写入。

## 凭证与锁

继续使用生产 `id:auth_version` 会话格式。新重置、邮件找回和注销通过 revoke_tokens 递增认证版本，并撤销 API token、小程序会话、跨端绑定挑战、旧 PairLink/行动令牌与短码。MiniProgramIdentity 原绑定版本不随改密迁移，因此旧微信绑定必须重新串联。个人改密与管理员旧入口必须调用同一撤销服务一次，不再重复自增。多账号恢复按编号锁住双方，重新验证操作者当前密码及当前版本后才写入，不能由家庭档案推断恢复授权。

注销继续调用生产统一匿名化流程；prepare_account_deletion 是其新表清理钩子，Web 和小程序均须接入。保留 deleted_at 墓碑，禁止引入替代 closed_at。管理账号先由其他管理员解除职责后使用自助注销，避免绕过最后管理员保护。

## 邮件配置与实际投递

邮件默认关闭。启用需项目配置：ACCOUNT_MAIL_ENABLED、ACCOUNT_SMTP_HOST、ACCOUNT_SMTP_PORT、ACCOUNT_SMTP_SSL、ACCOUNT_SMTP_USERNAME、ACCOUNT_SMTP_PASSWORD、ACCOUNT_MAIL_FROM 与 HTTPS PUBLIC_BASE_URL。SSL=false 强制 STARTTLS，不允许明文认证；不使用请求 Host 构造邮件链接。使用 Python 标准库 smtplib/ssl/email，无新增运行时依赖。真实 SMTP、DNS/SPF/DKIM/DMARC 和实际收件结果需部署操作者验证，单元测试 fake sender 不代表真实投递。

本人先登录、输入当前密码验证邮箱，再可找回；15分钟随机单次令牌只保存摘要，绑定用途、邮箱和当前密码。令牌存于链接片段，页面移出地址栏；密码提交有 CSRF，找回入口有IP限速与统一防枚举文案。未启用配置时界面明确说明不会发送，不宣称发送成功。配置 secret 不应进入日志、测试报告或Git。

## 上线冒烟

- 原有账号/remember cookie仍能正常读取，已注销账号继续拒绝；旧密码可登录且数据主键保持。
- 单独健康同意默认不选；未授权成员资料不可评分；Web撤回后小程序也拒绝健康读写，反向撤回同样有效。
- 真实已授权家属可以重置，未授权/跨社区/错误操作者密码/无CSRF拒绝；审计不含密码或健康内容。
- 管理员旧入口、自助改密、家属及邮件重置均让旧Web/remember/API/微信session/绑定挑战/旧短码失效。
- 已验证邮箱可收到实际验证及重置邮件；单次/过期/改邮箱/改密/注销后的旧链接拒绝；不存在邮箱响应相同。
- 测试账号注销清理新恢复/邮件/CoolingFeedback记录和既有私密资料，不影响其他账号；不要对真实用户做破坏性冒烟。

隐私说明仍需运营方确认处理者联系方式和备份保留期限；现有页面提供联系入口，但不把代码测试称为法律合规认证。
