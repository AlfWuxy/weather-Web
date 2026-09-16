# 机构数据接入与本地模型更新试点

本手册描述可以部署和核验的工程流程。文中的服务账户、目录、机构名称及 ID 变量都是占位；实际配置与执行记录放在受控私有运维位置。不要将病历、真实配置、训练 ZIP、候选模型包、截图或验收附件提交到产品仓库。

## 运行组成与数据边界

网页使用 `core.app:create_app`，后台使用 `pilot_worker:celery`。网页和 worker 共享站点数据库、机构权限、外置加密目录与密钥；Celery 使用独立的 `institution-pilot` 队列，只传数据库任务 ID，不传原始 Excel、就诊行、模型参数或密钥。Redis 不保存任务结果正文，任务状态以数据库 `pilot_jobs` 为准。

原件和冻结训练包由 Fernet 加密后存入 `PILOT_STORAGE_DIR`，目录必须位于代码和静态资源目录之外。随机文件名、目录 `0700`、文件 `0600`；机构内部就诊编号使用带密钥 HMAC。应用数据库仍含授权账户、机构、必要分析字段与版本记录，不能把“原件已加密”等同于整个数据库、所有备份均已加密。数据库和备份继续采用站点既有的私有访问与加密安排。

角色由机构成员关系决定：`uploader` 可读取、上传和导出，`researcher` 另可管理模型，`manager` 可管理本机构。站点管理员身份不能替代机构授权。机构所在地与患者居住地区划分别记录，缺少居住地不推断。

每日 08:00（Asia/Shanghai）调度天气更新与预报采集；每 60 秒扫描持久化队列恢复任务。机构启用后即可保存真实预报回执，没有已启用模型时不生成计数预测。Excel 上传后的处理由 worker 执行，病历的新鲜度取决于机构真实报送频率。

## 部署、迁移与关闭

1. 确认实际运行代码版本、数据库类型及路径、当前 Alembic revision、未提交变动、备份恢复流程。使用已审核的干净发布副本。此模块新增 `pilot_*` 表，迁移为 `0033_institution_data_pilot`，父版本为 `0032_weather_alert_provenance`；不调用清空旧病历的导入器，也不重建既有业务表。
2. 先保持 `FEATURE_INSTITUTION_WORKBENCH=0`，停止试点 beat 和 worker。迁移前制作一致性数据库备份并在隔离环境验证可读取、可恢复；若已有试点记录，同步保存加密目录及对应密钥的恢复副本。密钥备份与数据备份分开受控保存。SQLite 应使用备份 API 或 `.backup`，不能只复制运行中的数据库主文件而遗漏 WAL。具体备份路径和操作记录放私有运维位置。
3. 安装主项目已锁定的部署依赖；不要用本地训练的精简 requirements 替代服务依赖。在运行账户下准备 `/var/lib/example-pilot/data`、`state`、`config`、`secrets`，目录均为 `0700`；三个配置文件均为 `0600`。将服务模板中的 `exampleapp` 替换为已经批准的非 root 运行身份。
4. `/var/lib/example-pilot/config/app.env` 载入站点原有数据库、鉴权等配置；`config/pilot.env` 来自 `deploy/pilot.env.example`；`secrets/pilot.env` 只保存实际 `PILOT_STORAGE_KEY` 和 `PILOT_REDIS_URL`。基础 `app.env` 不得再定义试点字段：应用会读取 `CASE_WEATHER_ENV_FILE` 并覆盖同名环境变量，重复定义可能覆盖 systemd 后加载的值。
5. 新试点首次生成一次 Fernet 密钥，直接写入私有密钥文件，不打印到终端或命令历史。已有试点必须继续使用原密钥。密钥更换会使原件、快照无法读取，并影响身份去重摘要；当前版本未实现在线密钥轮换。
6. Redis 仅向授权主机/进程开放；公网不可访问，启用适用的 ACL、认证及持久化，跨主机连接采用 TLS。不要把已有业务 Redis 清空或改成试点专用。正式运行不得启用仅供测试的 `PILOT_TASKS_EAGER`。

首次部署可在已准备好的私有目录中执行以下一次性密钥生成；文件若已存在则拒绝覆盖。完成后通过受控配置编辑填写该文件中的真实 Redis 连接，密钥本身不需复制到终端：

```bash
/srv/example-app/.venv/bin/python - <<'PY'
import os
from cryptography.fernet import Fernet

# 独立文件、排他创建，不输出密钥，也不替换既有密钥。
secret_path = '/var/lib/example-pilot/secrets/pilot.env'
descriptor = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, 'wb') as handle:
    handle.write(b'PILOT_STORAGE_KEY=' + Fernet.generate_key() + b'\nPILOT_REDIS_URL=\n')
PY
```

以下命令应在经过授权的运行账户环境执行。辅助函数只读取受控配置文件，不在命令行展开秘密值：

```bash
cd /srv/example-app
pilot_exec() {
  /srv/example-app/.venv/bin/python -m dotenv -f /var/lib/example-pilot/config/app.env run -- \
    /srv/example-app/.venv/bin/python -m dotenv -f /var/lib/example-pilot/config/pilot.env run -- \
    /srv/example-app/.venv/bin/python -m dotenv -f /var/lib/example-pilot/secrets/pilot.env run -- "$@"
}
pilot_exec /srv/example-app/.venv/bin/alembic current
pilot_exec /srv/example-app/.venv/bin/alembic upgrade 0033_institution_data_pilot
pilot_exec /srv/example-app/.venv/bin/alembic current
pilot_exec /srv/example-app/.venv/bin/flask --app core.app:create_app pilot check
```

只有确认当前数据库处于父版本且备份验收完成，才执行上述升级。若 revision 更早、存在多头或数据库与记录不一致，先核实迁移链，不能 `stamp head` 掩盖差异。空白数据库初始化沿用站点流程；不要把本手册的单步升级用来替代初始化。

将两个 `.service.example` 安装为经审核的 systemd 单元；worker 先用单实例、`--concurrency=1`，beat 必须单实例，不另外启动 `worker -B`。队列连接、数据库和目录配置通过 `pilot check` 后，再启动 worker/beat。网页服务也必须注入相同三个环境文件。所有进程重启后配置才一致；确认机构授权和存储安排后才将开关设为 `1`。

正式上传前检查 `/workbench`：未登录不可访问，无机构授权不可读取；不同机构切换身份后不能读取彼此的原件、批次、快照、模型、预测或任务。反向代理对这些路径禁用缓存、请求正文记录与外部分析采集，并允许受限大小的 multipart 请求；应用 `.xlsx` 上限为 10 MiB，工作台 multipart 总上限为 20 MiB，JSON 上限为 2 MiB。不得为了试点扩大旧接口限额。

关闭功能时，将三个进程实际使用的开关设为 `0`，重启网页并停止 beat；让正在处理的 worker 安全退出。确认 `/workbench` 与 `/api/v1/workbench/*` 返回不可用。保留新增表、加密文件、哈希和审计记录；旧 RR 模型接口仍由原服务提供。迁移降级会拒绝删除含数据的试点表，正常回退采用关闭开关和回退应用代码，不执行破坏性删表。

## 两家合作机构开户与报送

先由既有账户流程建立并核验真实合作账户，再通过运维 CLI 授予机构权限。以下变量必须来自已确认的真实合作安排；机构名、地区代码、服务范围坐标、经理账户和原件保存期限均逐机构核对，不能用同一批演示数据冒充两家真实机构。

```bash
pilot_exec /srv/example-app/.venv/bin/flask --app core.app:create_app pilot institution \
  --name "${PILOT_A_NAME:?需真实机构名称}" --region-code "${PILOT_A_REGION:?需六位地区代码}" \
  --latitude "${PILOT_A_LAT:?需服务范围纬度}" --longitude "${PILOT_A_LON:?需服务范围经度}" \
  --manager-user-id "${PILOT_A_MANAGER_ID:?需既有有效账户ID}" \
  --retention-days "${PILOT_A_RETENTION:?需约定保存天数}" --storage-approved --enable

pilot_exec /srv/example-app/.venv/bin/flask --app core.app:create_app pilot institution \
  --name "${PILOT_B_NAME:?需真实机构名称}" --region-code "${PILOT_B_REGION:?需六位地区代码}" \
  --latitude "${PILOT_B_LAT:?需服务范围纬度}" --longitude "${PILOT_B_LON:?需服务范围经度}" \
  --manager-user-id "${PILOT_B_MANAGER_ID:?需既有有效账户ID}" \
  --retention-days "${PILOT_B_RETENTION:?需约定保存天数}" --storage-approved --enable

pilot_exec /srv/example-app/.venv/bin/flask --app core.app:create_app pilot member \
  --institution-id "${PILOT_INSTITUTION_ID:?需CLI返回的机构ID}" \
  --user-id "${PILOT_UPLOADER_ID:?需医生的既有账户ID}" --role uploader
pilot_exec /srv/example-app/.venv/bin/flask --app core.app:create_app pilot member \
  --institution-id "${PILOT_INSTITUTION_ID:?需机构ID}" \
  --user-id "${PILOT_RESEARCHER_ID:?需研究管理账户ID}" --role researcher
```

`institution` 每次创建新机构，不是可反复运行的更新命令。将两次返回的不同机构 ID 保存到私有开户记录，再分别为医生及研究账户添加成员关系。撤销访问使用 `pilot member ... --role <原角色> --revoke`；不要注销共用站点账户来替代单机构撤权。`--storage-approved` 表示已确认合作范围、存储位置及保存安排，不能仅为了通过校验而勾选。

医生登录后依次选择机构、上传单工作表 `.xlsx`、填写覆盖日期和完整/部分报送、列出停诊日、确认字段及异常。完整报送且没有有效记录的日期计为零；未报送、部分报送及停诊保持不同状态。缺失诊断需要明确确认，日期/年龄异常需修正；没有可靠就诊编号的疑似重复由医生核对。批次确认后追加，整批修订生成新版本，之前冻结的训练包不改变。

保存期限处理先预览，再按已批准的保存安排执行；不能把任务定时清理等同于法律上的删除证明：

```bash
pilot_exec /srv/example-app/.venv/bin/flask --app core.app:create_app pilot expire-originals \
  --institution-id "${PILOT_INSTITUTION_ID:?需机构ID}"
# 复核预览数量、期限依据和恢复需求后才执行下一条。
pilot_exec /srv/example-app/.venv/bin/flask --app core.app:create_app pilot expire-originals \
  --institution-id "${PILOT_INSTITUTION_ID:?需机构ID}" --apply
```

该命令处理已确认且到期的加密原件，保留批次哈希、分析记录及快照；不自动清除备份、未确认原件或派生文件。原件到期后的修订需重新取得有效原表。快照、未确认文件及备份的保存期限需要在合作安排中另行管理。

## 导出、本地训练与人工更新

在工作台创建固定截止日期的训练包，等待后台天气匹配及 ZIP 生成完成后下载。包内包含 `manifest.json`、`daily.csv`、`coverage.csv`、`dictionary.json`；日数据是本机构 60 岁及以上日就诊人次，不是独立患者数、发病率或个人患病概率。ERA5 是固定再分析天气产品，有发布延迟；缺少配套天气的日期不能借旧站其他天气源悄悄补齐。

本地用 Python 3.12 创建独立环境；下列命令中的数据目录仍为占位，须替换为获准保存的私有目录。此精简依赖文件只支持计数模型 CLI，不支持网页服务：

```bash
cd /srv/example-app
python3.12 -m venv /var/lib/example-pilot/local-train-venv
/var/lib/example-pilot/local-train-venv/bin/python -m pip install -r requirements-pilot-local.txt
/var/lib/example-pilot/local-train-venv/bin/python scripts/pilot_train.py \
  /var/lib/example-pilot/local-input/dataset.zip \
  --output /var/lib/example-pilot/local-output/candidate.json --name "本机构计数候选"
```

CLI 验证 ZIP 成员及逐文件哈希，不执行包内代码。固定的负二项计数模型比较日历基线与温度滞后候选，不使用昨天的病例数。最新 90 天作保留评估，之前 90 天作开发比较，更早的日期训练；温度 P10/P90 仅由训练期确定。开发集确定模型，保留集不参与重新拟合；七日累计标签不得跨划分边界。训练跨度或有效训练日不足一年时仅为探索结果，不能启用。

在模型页回传 `candidate.json`，服务端验证机构、地区、结局、年龄、快照哈希、日期、固定参数维度和七个一致性样例，并在服务器已保存的冻结数据上重算指标。服务器不接收 Python、pickle、RDS 或任意执行代码。候选不会自动启用；研究者查看同一快照/同一组日期的对照结果后手动启用。已经查看过的保留窗口不能作为新的独立验证重复使用；误差未改善时保留旧版本。回退只指向有记录的上一版本，保存操作人和前后版本。

未来预测从下一完整日开始，分别显示每日均值、第七日均值和未来七日累计均值。累计值相加未舍入的每日期望；不把每日区间上下界相加成累计区间。日区间为条件负二项预测区间，未涵盖参数和天气预报误差。真实预报产品与训练用 ERA5 分别记录，不宣称完全同源。

**评估证据限制：** 当前历史训练和回测标注 `retrospective_observed_weather`、`reporting_time_evidence=historical_availability_unverified`、`delay_replay_performed=false`。没有证据证明历史每一天的病历当时已可取得，也没有完成按历史报送延迟的回放。新上传的 `confirmed_at` 证明本次确认时间，不能反推旧就诊日的可用时间。前瞻评估只能使用从采集启用后实际保存、且接收时间不晚于发布预测时间的回执与预测，再等待真实报送结局进行匹配。

## 故障恢复与验收记录

| 现象 | 核对与恢复 |
|---|---|
| 网页任务排队，Redis 暂不可达 | 数据库已保存任务。恢复私有连接并检查 worker/beat；恢复扫描会重投排队任务。不要重新上传制造副本，也不要清空队列或数据库。 |
| worker 退出或任务卡住 | 当前硬时限为 15 分钟，超过 16 分钟未完成的运行任务可被恢复扫描接管，最多自动尝试 4 次。先核对原任务 ID、状态与权限，再从页面重试。 |
| Excel 异常或缺字段 | 进入批次核对日期、年龄、字段映射及重复。修复来源后重新解析/上传；不得把异常日期替换成今天。 |
| 天气缺失、接口超时 | 保留缺失与来源标记，修复连接后重试天气任务；ERA5 最近日期发布延迟不能靠切换产品掩盖。 |
| 预报不能产生预测 | 查看不可用原因和已保存回执。没有模型时仅保留天气回执；缺必要输入时不使用 RR 或默认病例数补位。失败重试必须保存新的真实接收时间，不能改写旧记录。 |
| 原件或快照无法解密 | 检查运行进程使用的是同一密钥和目录；从匹配的受控备份恢复。不要生成新密钥覆盖旧密钥。 |
| 账户撤权后任务失败 | 后台会重新检查机构成员关系；由授权人员核对后重新授予或停止任务，不能通过管理员身份绕过。 |
| 模型包被拒绝 | 检查快照身份、截止日期、哈希和指标；用原冻结包重新运行固定 CLI，不手工改模型 JSON 让它通过。 |
| 新版本表现不佳 | 保留比较证据，手动回退上一模型。数据和预测记录不删除，不把失败版本从历史中移除。 |

在隔离环境执行相关测试并填写实际结果，不把下面空白模板当作已经完成。Linux systemd、真实 Redis 网络、备份恢复、机构实际报送和医学解释都需要各自的验收证据。

```bash
cd /srv/example-app
.venv/bin/python -m pytest tests/test_pilot_data.py tests/test_pilot_models.py tests/test_pilot_workbench.py -q
```

以下记录模板复制到私有验收目录。机构使用授权代码；只保存必要汇总、哈希和内部引用，不粘贴患者内容。

| 验收项 | 私有记录内容 | 实测结果/阻塞/责任人 |
|---|---|---|
| 发布基线与迁移 | 代码版本、迁移前后 revision、备份引用、隔离恢复结果、开启/关闭时间 | 待填写 |
| 队列与配置 | 网页/worker/beat 配置一致性、beat 单实例、Redis 权限、连接中断与恢复任务 ID | 待填写 |
| 机构甲开户 | 合作授权引用、机构 ID、成员角色、保存期限、经办确认时间 | 待填写 |
| 机构乙开户 | 不同真实机构 ID、独立账户授权、保存安排、经办确认时间 | 待填写 |
| 跨机构隔离 | 页面/API/原件/批次/快照/模型/任务逐项拒绝结果、撤权后任务结果 | 待填写 |
| 甲第一轮/第二轮 | 各轮真实报送日期、覆盖范围、批次 ID/哈希、异常与修订、医生反馈 | 待填写 |
| 乙第一轮/第二轮 | 同上；不能复用甲的原始数据冒充报送 | 待填写 |
| 数据一致性 | 月度合计、年龄边界、重复、部分报送、零就诊、停诊、缺天气的核对结果 | 待填写 |
| 本地训练闭环 | ZIP 哈希、CLI/依赖版本、输出哈希、划分日期、探索状态、金样本及指标重算 | 待填写 |
| 人工启用与回退 | 同窗比较、操作人、候选/当前版本、启用与回退记录、实际使用范围 | 待填写 |
| 前瞻时间证据 | 真实回执 ID、received_at、issued_at、模型 ID、缺失/失败、随后病例匹配结果 | 待填写 |
| 到期处理与恢复 | 原件预览数量、期限依据、实际清理记录、备份独立保存安排、恢复演练 | 待填写 |
| 对外可说的结论 | 已完成事实、尚未完成事项、使用反馈与模型性能分开写 | 待填写 |

本地工程测试通过不等于“两家真实机构各完成两轮报送”，也不等于全国适用、临床有效或健康改善。新地区可以复用接入流程，但需分别核对数据口径、报送完整性和模型表现；未经验证地区只展示数据及探索性结果。
