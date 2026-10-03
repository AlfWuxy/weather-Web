# 部署安全前置条件

`scripts/deploy.sh` 是经审核的新服务器安装入口；已有不可变 release、独立迁移链或现场补丁的环境必须继续使用其发布事务，不得用本脚本全量覆盖。`scripts/sync.sh` 只接受已经通过相同安全边界检查的环境。

执行前配置 `DEPLOY_SERVER`、`DEPLOY_USER`、`DEPLOY_PROJECT_DIR`、`PUBLIC_BASE_URL`、`DEPLOY_ORIGIN_ADDRESS`、`DEPLOY_TLS_CERT_FILE`、`DEPLOY_TLS_KEY_FILE`。定位只放在本地私有环境文件，不提交真实目标。项目与证书路径使用无空白的绝对路径。项目不能位于 `/home`、`/root`、`/run/user` 或通过别名指向这些被 `ProtectHome` 隐藏的路径。

- `PUBLIC_BASE_URL` 必须是 HTTPS 域名且使用标准443端口。预置对应域名、完整信任链且未到期的证书和私钥；脚本不自动申请证书，也不开放临时明文应用入口。旧 `ALLOW_INSECURE_PUBLIC_BASE_URL` 不再构成部署放行条件。
- SSH Key 和 `CW_SSH_KNOWN_HOSTS` 必须通过可信渠道预置。默认使用当前用户的 `~/.ssh/known_hosts`，严格校验主机身份；未知密钥、变更密钥、空信任文件直接失败。不得仅依赖未经核验的 `ssh-keyscan` 输出建立信任。密码、自动接受密钥和任意 `SSH_OPTS` 覆盖已移除。
- 安装需要具备 root 权限的部署账号；应用与四类运行服务使用专用 `DEPLOY_APP_USER`（默认 `case-weather`），拒绝 UID 0。代码由 root 持有；`.env` 为运行账号所有的0600文件。systemd 将环境和代码只读挂载，仅开放 `instance`、`storage`、`logs` 写入，进程默认权限掩码0077。
- Gunicorn 固定监听 `127.0.0.1:5000`。Nginx 只通过回环地址访问应用；80端口只重定向到HTTPS。部署就绪需同时通过 Nginx 生效配置检查、源站本机TLS主机名与信任链校验、公网HTTPS健康检查、服务身份与监听检查，以及部署机对实际公网源站地址5000端口的隔离检查。CDN地址不能替代 `DEPLOY_ORIGIN_ADDRESS`。无法完成检查时脚本返回非零，不能宣称发布成功。
- Nginx配置与证书须适合目标环境；代理或证书检查失败不会自动回退到HTTP。安装器不是事务式发布器，失败后应检查当前阶段及服务状态再恢复；不要盲目重跑或绕过门禁。

环境文件写入统一使用 `scripts/secure_environment.py`：已有文件先纠正权限，拒绝符号链接与硬链接；临时文件创建即0600，原子替换保留属主。部署秘密只经SSH标准输入发送，不进入远端命令参数。运行配置有明确白名单，包含拆分后的地图密钥、资源预算与Token期限，不携带部署密码或主机定位。非空运行参数可更新默认值，已有身份和提供方密钥保持不变；密钥轮换需单独执行。部署固定生产模式且关闭DEBUG与明文豁免。配置不支持多行值或需要转义的引号；无效输入返回失败，不拼接执行。

备份仍保留SQLite一致性备份与30天清理行为，只处理显式项目根目录对应的数据库。备份根目录及子目录0700、现有和新增文件0600，拒绝链接，输出文件独占创建；整个备份树必须由备份执行账号持有。root运行时也不会用chmod接受其他属主，发现错属主会停止，先单独审核归属再处理。非root运行的备份归该非root执行账号，不能将应用账号目录当作root私有备份库。权限收紧不会将备份复制到公共位置。

本地验证使用临时数据库及SSH、rsync、TLS与socket桩；不会运行真实部署、迁移或服务重启。运行 `python -m pytest -q tests/test_deployment_security.py tests/test_deploy_script.py tests/test_sync_script.py tests/test_manual_fix_script.py tests/test_sqlite_path_resolution.py`，并对所有改动的Shell脚本执行 `bash -n`。
