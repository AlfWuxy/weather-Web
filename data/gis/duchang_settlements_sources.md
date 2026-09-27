# 都昌县公开聚落点：来源、筛选与质量边界

生成/检索记录：2026-09-27T04:15:36.100384+00:00

本次共 486 个聚落候选点，空间分布覆盖 24 个乡镇；这不是官方村庄总数，也不是完整覆盖率。

## 数据与许可

- OpenStreetMap：只使用具名 `place=village` / `place=hamlet` 原始节点，WGS84，标为 `mapped_point`；该标签反映聚落，不直接证明自然村或行政村级别；mapped_point仅表示原始地图点，不表示GPS精度或现场核验。保留 OSM 对象链接和最后编辑时间。© OpenStreetMap contributors，ODbL 1.0。
- GeoNames：中国公开地名库 `CN.zip`，只使用 `PPL`（populated place）记录，WGS84，标为 `approximate`。明确排除 `PPLA*` 驻地、`PPLF` 农场等；不少条目最后更新于2021或2023年。GeoNames，CC BY 4.0。
- 下载/检索日期与每条记录的最后更新时间分别保存；下载于2026年并不意味着地名、位置已在2026年复核。两源可能共同来自GNS或互相引用，不当作两个独立实测证据。
- GeoNames不保证准确性、及时性、完整性；OSM是社区地图。名称含“村”也不足以确认行政级别，所有 `settlement_level` 保持 `unknown`，人口、老年比例保持 `null`。
- 合并派生数据库保留ODbL 1.0与OpenStreetMap署名，同时保留GeoNames CC BY 4.0署名；逐点 `sources` 可追溯贡献来源。

公开来源：[OSM标签说明](https://wiki.openstreetmap.org/wiki/Key:place)、[OSM许可](https://www.openstreetmap.org/copyright)、[GeoNames完整字段/许可说明](https://download.geonames.org/export/dump/readme.txt)、[GeoNames类型](https://www.geonames.org/export/codes.html)、[本次下载](https://download.geonames.org/export/dump/CN.zip)。

## 获取与空间筛选

- OSM端点：`https://overpass-api.de/api/interpreter`；OSM数据基准时间 `2026-09-27T04:04:35Z`。
- 查询：`[out:json][timeout:25];node["place"~"^(village|hamlet)$"](29.05507,116.02658,29.63809,116.64172);out meta;`。只查询节点，不把村级边界的包围盒中心伪装为村庄坐标。
- GeoNames文件HTTP Last-Modified：`Sun, 27 Sep 2026 02:40:54 GMT`；SHA-256：`ff60ed6ea5d5cf5c72994c75db1bbd35ff7be20551d875c901dc90c6590bfd6d`。
- 乡界：仓库 `data/gis/duchang_townships_osm.geojson`，SHA-256 `7863a2dfdef89387fe0813a891dc9a80d38a74095d3c1aa438461823d3bab527`，2026-09-26来源快照。
- 只集成唯一落入上述24乡界的点。县外包框中的县外记录排除；乡界缝隙、交叠或边界归属不能唯一判断的记录保留在研究排除表中，未按最近乡驻地猜配。乡界及村点都不作为法定行政归属证明。
- 未引入项目原先人工维护的GCJ-02村点，避免把维护数据误标为公共来源；所有本文件坐标直接来自WGS84公开源。

## 去重与残余疑点

- 原始唯一归属记录 527 条，合并减少 41 条；同乡、同名或别名（仅去末尾“村”并统一拉丁大小写）、簇内所有坐标两两不超过150米才合并。
- 合并优先保留OSM原始点坐标；每条来源原来的名称、坐标、更新时间都保存在 `properties.sources`，不平均坐标。
- 超过去重阈值的跨源同乡同名点继续分别保留，38个点标记 `potential_duplicate=true` 并列出关联ID；可能是坐标误差、行政村/自然村同名或两个不同聚落，需人工核查。
- 不依据聚落规模标签猜行政/自然村，不生成未出现于来源中的名称和坐标。未经村委或实地核验，本文件用于辅助地图与候选检索。

## 乡镇分布（仅本数据集点数）

| 乡镇 | 原始OSM | 原始GeoNames PPL | 去重后候选点 |
|---|---:|---:|---:|
| 万户镇 | 1 | 15 | 16 |
| 三汊港镇 | 0 | 13 | 13 |
| 中馆镇 | 5 | 16 | 18 |
| 北山乡 | 4 | 26 | 30 |
| 南峰镇 | 0 | 8 | 8 |
| 周溪镇 | 0 | 31 | 31 |
| 和合乡 | 0 | 19 | 19 |
| 土塘镇 | 15 | 30 | 38 |
| 多宝乡 | 0 | 15 | 15 |
| 大树乡 | 4 | 15 | 15 |
| 大沙镇 | 0 | 20 | 20 |
| 大港镇 | 5 | 23 | 25 |
| 左里镇 | 2 | 16 | 16 |
| 徐埠镇 | 1 | 23 | 23 |
| 春桥乡 | 2 | 12 | 13 |
| 汪墩乡 | 20 | 41 | 51 |
| 狮山乡 | 0 | 13 | 13 |
| 芗溪乡 | 0 | 10 | 10 |
| 苏山乡 | 0 | 16 | 16 |
| 蔡岭镇 | 3 | 23 | 24 |
| 西源乡 | 1 | 16 | 17 |
| 都昌镇 | 0 | 21 | 21 |
| 阳峰乡 | 2 | 16 | 16 |
| 鸣山乡 | 8 | 16 | 18 |

## 可复核原件

原始文件另存本地研究归档，不作运行依赖；原件文件名与指纹用于对应本轮快照：
- `osm_duchang_bbox_settlements_raw.json`：Overpass原始节点、原始标签、查询和检索时间。
- `geonames_CN.zip`：官方整包，与上面SHA-256对应。
- `geonames_duchang_bbox_raw.json`：县外包框内原始字段。
- `county_settlements_excluded.json`：无名、非村类型、县外和边界待核验排除记录。
- `county_settlements_summary.json`：原始来源数量、去重和乡镇分布。
- `scripts/gis/build_public_settlements.py`：仓库离线维护脚本，显式输入原始文件路径；默认无联网且不写原件。

排除原因汇总：`{"outside_study_boundary": 290, "unnamed_or_not_village_or_township_seat": 12, "geonames_not_PPL": 71}`。


## 离线复建

从研究归档取得原始输入后执行（路径为示例占位符）：

```bash
python scripts/gis/build_public_settlements.py \
  --osm /path/to/osm_duchang_bbox_settlements_raw.json \
  --geonames /path/to/geonames_duchang_bbox_raw.json \
  --output /path/to/duchang_settlements.geojson \
  --sources-output /path/to/duchang_settlements_sources.md
```

也支持直接读取GeoNames CN.zip或CN.txt，此时必须用 --retrieved-at 明确原始下载时间。脚本只离线读取输入；不会自行请求API、更新原件或覆盖输入。--townships/--county-cells 可指定边界快照；--report-dir 可输出排除记录与分布统计。

## 本次输入原件指纹

- `osm_duchang_bbox_settlements_raw.json`：SHA-256 `429334bafb5e08844c75ac4a6b66c1f8d92f7c2969eae6fd37e023cba7559b5c`。
- `geonames_duchang_bbox_raw.json`：SHA-256 `4f897293cc6cfefb6b64b43e8247a39cd89c077897aedff0c84969018d95463b`。
- `duchang_townships_osm.geojson`：SHA-256 `7863a2dfdef89387fe0813a891dc9a80d38a74095d3c1aa438461823d3bab527`。
- `duchang_heat_exposure_cells.geojson`：SHA-256 `c72b6d1a9ac9ba2c53bdf3ffdc1aadb14775c15ced2e156674e64be3a8a40237`。
