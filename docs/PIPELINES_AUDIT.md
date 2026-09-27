# 离线脚本调查报告（精简方案 P6）

> 调查对象：`services/pipelines/` 下全部 12 个脚本（共 2938 行）· 基线：`main@0d8deda` · 日期：2026-09-27
> 本报告只记录事实和建议，不删除、不修改任何脚本。去留由负责人决定（第 5 节）。

## 结论速览

- **12 个脚本分三类**：训练 5 个、分析与导入 3 个、定时任务 4 个。定时任务里有 3 个由 `scripts/deploy.sh` 装成 systemd 定时器，属于生产链路，必须保留。
- **训练脚本在仓库里一个都跑不起来**：它们都依赖病历表 `data/research/数据.xlsx`，这个文件含患者个人信息，被 `.gitignore` 和部署脚本有意排除。其中两个还依赖 `requirements.txt` 里没有的包。
- **线上模型的来源无法从仓库证明**：应用加载的 3 个 `.pkl` 模型文件不在仓库里，也没有训练记录。仓库里的 `models/feature_config.json` 与 `train_multiclass_model.py` 的输出结构逐项一致，只能说明它最可能出自这个脚本。
- **git 历史帮不上忙**：12 个脚本最早都出现在 2026-02-03 的整体导入提交 `5df51e6 init` 里；5 个训练脚本此后从未修改。
- **调查中发现 8 个问题**（第 4 节），包括极端天气标记在两条写入路径上恒为否、社区风险预热的缓存时长配置没有生效。按执行纪律，这些问题只记录，另开 PR 处理。

## 1. 调查方法

| 手段 | 做法 |
|---|---|
| 读代码 | 逐个脚本核对输入、输出、调用方式，结论都附 `文件:行号` |
| 全仓库搜索 | 代码、模板、测试、脚本、文档、systemd、CI 配置里的全部引用 |
| 实际运行 | 在隔离的仓库副本中断网运行 `import` 和 `--help`，记录报错；不在主仓库运行 |
| git 历史 | 每个脚本的首次出现、最近修改时间与作者 |
| 与线上加载代码对照 | `services/ml_prediction_service.py`、`services/dlnm_risk_service.py` 读取哪些文件 |

凡是无法验证的内容，都标注为"未核实"。

## 2. 总表

| 脚本 | 行数 | 类别 | 输入是否在仓库 | 主要输出 | 调用方式 | 测试 | 在仓库里能否运行 |
|---|---|---|---|---|---|---|---|
| `train_binary_model.py` | 271 | 训练 | 否（缺病历表） | `models/` 下 4 个文件 | 无入口，导入即运行 | 无 | 否 |
| `train_multiclass_model.py` | 264 | 训练 | 部分（天气 CSV 在，病历表缺） | 同上 | `__main__` 入口 | 无 | 否（缺病历表） |
| `train_optimized_model.py` | 334 | 训练 | 否 | 同上 | 无入口 | 无 | 否（还缺 `imbalanced-learn`） |
| `train_real_model.py` | 373 | 训练 | 否 | 同上 | 无入口 | 无 | 否 |
| `train_xgboost_model.py` | 198 | 训练 | 否 | 同上 | 无入口 | 无 | 否（还缺 `xgboost`） |
| `analyze_for_model.py` | 89 | 分析 | 否（缺病历表） | 只打印 | 无入口，导入即运行 | 无 | 否 |
| `analyze_surnames.py` | 98 | 分析 | 读数据库 `medical_records` | 只打印 | `__main__` 入口 | 入口契约测试 | 需要已导入病历的数据库 |
| `import_data.py` | 499 | 导入 | 否（缺病历表） | 清空并重写 3 张表 | `__main__`；`scripts/start.bat` 调用 | 入口契约测试 | 否 |
| `sync_weather_data.py` | 465 | 定时 | 是 | 天气、每日状态、社区日统计 | 命令行；只有 cron 示例 | 4 个功能测试 | 是 |
| `sync_weather_cache.py` | 138 | 定时 | 是 | 天气缓存、天气表 | systemd，每 30 分钟 | 只测地点解析 | 是 |
| `dispatch_alerts.py` | 33 | 定时 | 是 | 预警、推送记录、微信推送 | systemd，每 30 分钟 | 4 个功能测试 | 是 |
| `precompute_community_risk.py` | 176 | 定时 | 是 | 社区风险缓存 | systemd，每 60 分钟 | 3 个功能测试 | 是 |

"入口契约测试"指 `tests/test_pipeline_entrypoints.py`。它用 `runpy.run_path(..., run_name='pipeline_entrypoint_contract')` 加载脚本（第 45 到 69 行），`__main__` 分支不会执行，所以只证明脚本能被导入，不证明脚本能正常工作。

## 3. 逐类说明

### 3.1 训练脚本（5 个，共 1440 行）

**共同事实**

- 输入：都用 `header=None` 读 `data/research/数据.xlsx`，并写死 15 个列名（序号、医保、姓名、性别、年龄、就诊时间、科室、医生、疾病分类、主诉、病历描述等）。这是含患者姓名和病史的个人健康数据。该目录不在仓库里，`.gitignore:77` 忽略 `data/research/*.xlsx`，`scripts/deploy.sh:149-150,176,192` 同步到服务器时也排除它。
- 输出：5 个脚本写同一组文件：`models/disease_predictor.pkl`、`label_encoder.pkl`、`scaler.pkl`、`feature_config.json`。谁最后运行，谁的结果就覆盖其他脚本的。仓库里只有 `feature_config.json`；3 个 `.pkl` 被 `.gitignore:76` 忽略。
- 都没有命令行参数（`argparse` 出现 0 次）。除 `train_multiclass_model.py` 外都没有 `__main__` 入口，导入模块就会开始训练；因此 `--help` 也会直接开始训练。
- 仓库中没有任何测试、CI 步骤、shell 脚本或 systemd 单元调用它们。只有文档提到：`docs/PROJECT_CATALOG.md:33-37`（路径已过时），`PROJECT_OVERVIEW.md:118`（只提多分类脚本），`docs/ARCHITECTURE.md:104`（规划中的 `scripts/train_model.py`，实际不存在）。

**逐个说明**

| 脚本 | 预测目标与算法 | 特征数 | 额外依赖 | 与已提交配置的关系 |
|---|---|---|---|---|
| `train_binary_model.py` | 是否呼吸系统疾病；随机森林、梯度提升、极端随机树、AdaBoost 加软投票，取测试集准确率最高者 | 7 | 无 | 部分一致（类别不同，缺天气和特征重要性字段） |
| `train_multiclass_model.py` | 原始疾病分类（样本不少于 10 的类别）；随机森林 | 15（含 8 个天气特征，来自 `data/raw/逐日数据.csv`） | 无 | **结构逐项一致**：9 个键及顺序、15 个特征名、模型名、类型、描述、天气特征列表都相同 |
| `train_optimized_model.py` | 三类（呼吸、消化、其他）；SMOTE 过采样后比较随机森林、梯度提升、软投票 | 12 | `imbalanced-learn`（不在依赖清单） | 部分一致；应用端不会构造 12 个特征，这个模型即使训练出来也无法被加载使用（由代码推断，未运行验证） |
| `train_real_model.py` | 九类疾病大类；随机森林、梯度提升、逻辑回归，随机森林胜出时再做 108 组参数的网格搜索 | 7 | 无 | 部分一致（只有 5 个基本键） |
| `train_xgboost_model.py` | 三类；XGBoost | 7 | `xgboost`（不在依赖清单） | 部分一致（只有 5 个基本键） |

**能否复现 `models/feature_config.json`**

- 结构上，只有 `train_multiclass_model.py:238-249` 写出的配置与仓库文件逐项一致。
- 数值上无法复现：准确率 0.6527、F1 0.6418、特征重要性和 13 个类别名都依赖缺失的病历表。即使拿到数据，库版本不同也可能得到不同结果（未核实）。
- 这些只能说明配置文件的格式出自该脚本，不能证明线上模型由它训练。

**线上怎样使用模型**

- `services/ml_prediction_service.py:95-104` 用 joblib 加载 3 个 `.pkl`，并读取 `feature_config.json`；运行时的 scikit-learn 主次版本必须等于 `ML_EXPECTED_SKLEARN_VERSION`（默认 1.7.2，第 16 行），否则拒绝加载。
- 配置里含 `tmean` 时构造 15 个特征（对应多分类脚本），否则构造 7 个特征（对应二分类、真实、XGBoost 三个脚本）。应用还会读取 `model_type`、`weather_features`、`feature_importance`、`description`，这些字段只有多分类脚本会写。
- 调用方：`blueprints/tools.py:22,275`（机器学习预测页），`services/api_service.py:259,327,400`。
- `core/metric_explanations.py:349,362` 向用户展示"训练准确率约 65.3%，F1 约 64.2%"，与仓库配置一致。
- 另一个线上模型 `data/models/final_single_model_ar1_profile.json`（DLNM，`services/dlnm_risk_service.py:174` 加载）不由任何一个训练脚本生成，来源同样无法从仓库追溯。

**重复代码**

5 个脚本之间大段重复：读表与命名列（每个脚本约 4 行）、年龄解析（两个版本，各被 2 到 3 个脚本复制）、季节判断（5 份相同）、年龄分组（4 份相同）、性别归一（2 份）、标准化与切分数据集（5 份）、保存 4 个文件（5 份）、结尾的写死预测样例（4 份）。`train_binary_model.py:44-106` 与 `train_real_model.py:44-111` 除注释和空白外完全相同。

从代码看，`train_multiclass_model.py` 最完整：它是唯一有函数入口的、唯一使用天气数据的，也是唯一写出应用所需全部字段的。二分类和真实两个脚本的数据清洗更稳妥（处理空值、`errors='coerce'`），多分类脚本第 109 行缺少这一处理。

### 3.2 分析与导入脚本（3 个）

| 脚本 | 做什么 | 输入 | 输出 | 引用 |
|---|---|---|---|---|
| `analyze_for_model.py` | 探索性统计：疾病、科室、年龄、月份、性别分布 | 病历表（缺失，含个人信息） | 只打印 | 仅 `docs/PROJECT_CATALOG.md:31,168`；不在入口契约测试中 |
| `analyze_surnames.py` | 统计患者姓氏频次与社区分布，列出不在写死映射里的姓氏 | 数据库 `medical_records` 的 `patient_name`、`community`（个人信息） | 只打印 | `tests/test_pipeline_entrypoints.py:14`；`docs/PROJECT_CATALOG.md:32` |
| `import_data.py` | 把病历表导入 `medical_records`，按姓氏**随机**分配村庄；由病历生成社区；再写入 7 天**随机**天气 | 病历表（缺失） | 先清空再重写 `medical_records`、`communities`、`weather_data`；先执行 `db.create_all()` | `scripts/start.bat:35-37`（Windows 开发启动脚本，数据库不存在时调用）；入口契约测试第 16 行；3 处文档 |

- `analyze_for_model.py` 在模块层面直接读表，导入即报 `FileNotFoundError`。
- `import_data.py` 不是生产环境的初始化方式：文档记载的初始化是 `flask init-db`（`core/app.py:105-111`），部署脚本不上传病历表，它在服务器上无法运行。它会用随机天气覆盖定时任务同步的真实天气数据。
- 三个脚本都没有命令行参数；对 `import_data.py` 执行 `--help` 会直接建表并尝试清空数据。

### 3.3 定时任务（4 个）

| 脚本 | 生产调度 | 做什么 | 外部服务 |
|---|---|---|---|
| `sync_weather_cache.py` | `scripts/deploy.sh:323-350,421` 安装 systemd 定时器，每 30 分钟；`docs/systemd/` 有同样的单元文件 | 刷新天气缓存，并写当天的天气记录 | 和风天气（失败时退到 Open-Meteo 或模拟数据） |
| `dispatch_alerts.py` | `scripts/deploy.sh:354-379,427`，每 30 分钟，按 6 小时去重 | 选官方预警或阈值规则，通过 WxPusher 推送给照护人 | 和风天气预警、高德地理编码、WxPusher |
| `precompute_community_risk.py` | `scripts/deploy.sh:383-409,433`，每 60 分钟 | 预热社区风险地图的计算结果 | 和风天气 |
| `sync_weather_data.py` | **部署脚本没有安装**；只有 `docs/guides/heat_action_cron.txt:12-13` 里的 cron 示例（每天 7、12、18 点） | 回填历史天气 CSV；按和风天气写每日天气；更新配对的每日风险等级和社区日统计 | 和风天气 |

- 这 4 个脚本在测试环境变量下都能导入，`--help` 正常。前三个在空环境变量下导入会因缺少 `SECRET_KEY` 报错，因为模块层面就创建了应用；这是预期行为。
- 网页对它们只是"软依赖"：缓存未命中时网页会自己拉取和计算，社区统计缺失时会从每日状态重新计算。但"连续高温天数"只数已有的天气记录，如果定时器停了又没人访问页面，连续天数会被重置。

## 4. 调查中发现的问题（只记录，本轮不修改）

编号接着精简方案 6.1 节的 F1。

| 编号 | 问题 | 证据 | 影响 |
|---|---|---|---|
| F2 | 行动页风险同步（`sync_weather_data.py --daily --action-daily`）没有被部署脚本调度，只有 cron 示例 | `scripts/deploy.sh` 只装了另外 3 个定时器；`docs/guides/heat_action_cron.txt:13` | 若服务器上也没有手工配置 cron（未核实），每日天气和社区日统计只由网页访问触发 |
| F3 | 极端天气标记在两条写入路径上恒为否 | `sync_weather_data.py:337-338` 从 `get_current_weather()` 的结果取 `is_extreme`，但只有 `identify_extreme_weather()`（`services/weather_service.py:1203`）会产生这个字段；`sync_weather_cache.py:58-73` 不写这两个字段 | 缓存任务先写入当天记录时，仪表盘"极端天气自动生成预警"（`services/user/dashboard_service.py:373`）可能不会触发（由代码推断，未运行验证） |
| F4 | 天气缓存任务不校验数据来源，Open-Meteo 或模拟数据也会写进天气表 | `sync_weather_cache.py:99-110`；对比 `sync_weather_data.py:47-104,199-209` 会拒绝这类数据 | 天气表可能混入备用来源的数据 |
| F5 | 社区风险缓存时长配置没有生效 | `.env.example:102` 设 1500 秒，但 `core/config.py` 从不读取 `COMMUNITY_RISK_CACHE_TTL_SECONDS`，实际按 `services/community_risk_cache.py:120` 的默认 600 秒；定时器每 60 分钟预热一次；`.env.example:109` 又写"默认每 20 分钟" | 每小时约 50 分钟缓存是冷的；未配置 Redis 时预热结果只留在定时任务自己的进程里（生产是否配置 Redis 未核实） |
| F6 | 同一个县有两套地点名 | `sync_weather_data.py` 默认 `都昌`（`core/config.py` 的 `DEFAULT_CITY`），`sync_weather_cache.py:83` 默认 `都昌县` | 天气表里同一县有两条序列 |
| F7 | 8 个脚本（训练 5 个、分析与导入 3 个）没有命令行参数解析，`--help` 会直接执行 | 见 3.1、3.2 | `import_data.py` 会建表并清空数据 |
| F8 | 两个训练脚本依赖不在清单里的包 | `train_optimized_model.py:12`（`imblearn`），`train_xgboost_model.py:10`（`xgboost`） | 按依赖清单安装后无法运行 |
| F9 | 文档路径和行号过时 | `docs/PROJECT_CATALOG.md:29-37,166-178`；`docs/ARCHITECTURE.md:103-104` | 由 P7 文档同步一并处理 |

## 5. 去留建议（待负责人决定）

| 编号 | 对象 | 建议 | 理由 |
|---|---|---|---|
| D5 | `train_multiclass_model.py` | **保留**，作为唯一的训练入口 | 输出结构与仓库配置、应用加载逻辑逐项对应 |
| D6 | 其余 4 个训练脚本 | 负责人确认它们都不是线上模型的来源后，**归档**（移到 git 标签或归档分支，主分支删除，历史仍可找回）；确认前保留 | 在仓库里都跑不起来，会互相覆盖同一组文件；其中两个依赖不在清单里的包，一个的输出应用无法使用 |
| D7 | `analyze_for_model.py` | 同 D6 | 导入即运行、需要个人数据、没有任何代码或测试引用 |
| D8 | `analyze_surnames.py` | 保留；若将来归档，同时从入口契约测试中移除 | 被测试引用；输出含患者姓名统计 |
| D9 | `import_data.py` | **保留**，另开 PR 加一道确认（例如必须传 `--yes-wipe` 才会清空数据） | 开发环境启动脚本依赖它；它会清空 3 张表并写入随机天气，误执行代价大 |
| D10 | 4 个定时任务 | **全部保留**；F2 到 F6 各自另开 PR 修复 | 生产链路 |
| D11 | 训练溯源 | 今后训练时，把"脚本、数据文件哈希、库版本、时间"写进 `feature_config.json` | 解决"线上模型从哪来"无法回答的问题；应用已经在检查 scikit-learn 版本，可以顺带展示 |

需要负责人回答的问题：**线上服务器上的 3 个 `.pkl` 文件是用哪个脚本、哪份数据训练的？** 如果能确认是 `train_multiclass_model.py`，D6 和 D7 就可以执行。

## 6. 未核实事项

- 线上 `.pkl` 的训练来源（第 5 节的问题）。
- 服务器上是否另有 crontab 运行 `scripts/weather_sync.sh`（F2）。
- 生产环境是否配置了 `REDIS_URL`（F5）。
- F3 对仪表盘的实际影响（由代码推断，未在运行环境中复现）。
- 推送的预警能否在仪表盘的预警列表中显示（`location` 字段是否一致）。

## 附录：复现方法

```bash
# 在隔离副本中运行，避免训练脚本覆盖 models/ 下的文件、导入脚本清空数据库
git archive origin/main | tar -x -C /tmp/pipelines-audit && cd /tmp/pipelines-audit
export PYTHONDONTWRITEBYTECODE=1 DATABASE_URI=sqlite:////tmp/pipelines-audit/audit.db \
  SECRET_KEY=audit-only-secret-key-0123456789abcdef DEBUG=true DEMO_MODE=1
# 只加载模块，不执行 __main__ 分支
for f in services/pipelines/*.py; do
  python -c "import runpy; runpy.run_path('$f', run_name='audit')" >/dev/null 2>&1 \
    && echo "OK   $f" || echo "FAIL $f"
done
# 预期：analyze_for_model 与 4 个训练脚本（binary、optimized、real、xgboost）FAIL，其余 7 个 OK
python -c "import json; print(list(json.load(open('models/feature_config.json'))))"
sed -n 238,249p services/pipelines/train_multiclass_model.py
git log --reverse --format='%h %ad %s' --date=short -- services/pipelines/ | head -3
```
