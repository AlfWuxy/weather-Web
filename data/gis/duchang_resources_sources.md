# 都昌县医疗与避暑候选资源：公开点位来源说明

采集日期：2026-09-27。文件为 `data/gis/duchang_resources.geojson`，并与 `duchang_resource_inventory.json` 的官方机构表分开保存。

本版有 **25 个医疗点、2 个避暑候选地图点、0 个已确认当前开放避暑点**。医疗点按现有 OSM 乡界落入 **22 个乡镇**；鸣山未找到可信机构坐标，汪墩存在同名机构地点冲突，均没有用乡政府或乡镇中心代替。点位不是全县医疗机构普查，更不是当前营业或急救接诊能力的确认。

## 字段及事实边界

- `verification_status=publicly_listed` 仅表示在公开地图可查到该机构条目，不表示实地核验、仍在营业或保证可以接诊；全部 `current_opening_status=unknown`。
- 百度点保留具体门诊名：**北山乡卫生院-预防接种门诊**和**阳峰乡卫生院-发热门诊**。两者 `inventory_ids=[]`，不将科室点升级为整个卫生院，不从待定位表移除主院。
- **都昌县狮山医院**和**都昌县多宝乡寺前村卫生院**为地图原名。官方表分别有狮山乡卫生院、寺前村卫生所，但目前没有足够别名证据确认同一机构；这两点也未关联官方 ID。
- 其余 21 个医疗点使用逐项显式 `inventory_ids` 关联官方机构全称。匹配依据为公开名称、对应乡镇和可用地址；没有自动模糊字符串合并，也没有改写官方原表。该关联只用于同机构条目去重，不把 2026 医保公示日期当作坐标核验日期。
- `township_name` 来自 WGS84 点与现有 OSM 乡镇边界的几何归属。边界本身是社区制图资料，不能代替法定行政边界。
- 开放时间、空调、公众可进入、当前避暑开放有效期均为 `null`。个别地图展示过营业时间，但来源更新时间未知，本版没有将其转为当前保证。没有采集医生私人信息或评论。

## 来源、许可与坐标

### OpenStreetMap

[OSM 版权与 ODbL 说明](https://www.openstreetmap.org/copyright)。通过官方地图 API 返回的 XML 对象读取 WGS84；源对象最后编辑日期单独保留，不能当作本次现场核实日期。

- [蔡岭中心卫生院院区 way 1485918607](https://www.openstreetmap.org/way/1485918607)：取闭合院区面的几何质心，`coordinate_precision=approximate`，不是院门、救护入口或单栋建筑位置。
- [文化中心 library 建筑 way 618453861](https://www.openstreetmap.org/way/618453861)：取闭合建筑面的几何质心，`coordinate_precision=building_centroid`。OSM 最后编辑于 2018 年；目前仅是 `cooling_candidate`，没有证据确认其与官方报道的都昌县图书馆为同一建筑，也没有空调、免费纳凉、开放时间或公共进入保证。

其他 OSM 政府楼、税务所、体育场、敬老院没有因名称或建筑类别就被登记为开放避暑点；邻县设施按县界排除。早先两乡 Overpass 返回 0 条只表明地图覆盖空白，不代表真实资源不存在。

### 百度公开机构条目

通过百度地图普通网页检索、读取机构详情，再使用“到这去”对两个公开医疗机构作路线查询；读取网址中 `sn` 或 `en` 的**机构目标坐标**。没有采用网址 `@x,y,z` 的地图视野中心，没有调用隐藏页面状态、私有接口或登录开发者坐标拾取器。

[百度官方坐标系说明](https://lbsyun.baidu.com/index.php?title=jspopular/guide/coorinfo)及[官方投影说明](https://lbs.baidu.com/docs/jsapi?title=jsapi4%2Fguide%2Fconcept%2Fprojection)区分 BD09MC、BD09 经纬度及 EPSG:3857。原始目标是 **BD09MC**，没有直接当作 WGS84 或标准 Web Mercator 使用。

转换采用 [projzh 1.0.0](https://github.com/tschaub/projzh) 的 MIT 许可源码：`projection.baiduMercator.inverse` → `datum.bd09.toGCJ02` → `datum.gcj02.toWGS84`。每个百度点保留原始 MC、BD09、GCJ02、转换库版本和数学往返误差，均标 `approximate`。本轮数学往返误差最大约 2.83 米，仅检验转换链，不代表 POI 实地精度。链接规范化时只保留机构名和 UID，移除无关的视野中心、设备和跟踪参数；原始 UID 与目标参数文本仍保留。

百度地图与数据提供者保留其权利；公开网页不等于开放数据库许可证。本文件只记录本轮公开机构事实及归属证据，不附地图底图、评论或商铺数据库。它不继承项目代码的许可，也不被重新声明为 ODbL 或 MIT 数据。

### 高德和腾讯

[高德人民医院条目 B03180099C](https://ditu.amap.com/place/B03180099C)，源坐标 GCJ-02 `[116.198032,29.267878]`。[高德官方坐标系说明](https://lbs.amap.com/faq/advisory/others/39838)。

[腾讯中医院条目 4749403447891313513](https://map.qq.com/poi/?sm=4749403447891313513&keyfrom=1)，页面“搜索周边”链接 `where` 及小地图 `center` 一致为 GCJ-02 `[116.221275,29.279749]`。[腾讯官方坐标说明](https://lbs.qq.com/faq/latlngFaq)。二者均用项目已有 `gcj02_to_wgs84` 迭代近似反算，原始坐标和系别保留；数据来源没有提供开放数据库许可。

同院多源只保留一个代表点：蔡岭使用 OSM 院区质心，百度点作为另一项证据；人民医院使用高德点，百度点作为另一项证据。两源在蔡岭相差约 87 米、人民医院约 113 米，属于医院区域内不同代表点的可能差异，不能当作两家医院，亦不能保证具体入口。人民医院的高德门牌 75 号与百度及 2022 官方门牌 96 号不同，已在点属性提示，当前门牌待确认。

### 官方名录与历史避暑报道

[2026-06-18 都昌县医保局定点医疗机构公示](https://www.duchang.gov.cn/zwgk/zfxxgkzl/bmxxgk/ybj/fgzc/zcwj/202606/t20260618_7255873.html)用于机构收录与明确 ID 关联，不能证明当前营业。地址补充来自[2022-12-16 发热门诊公示](https://www.duchang.gov.cn/zwzx/gsgg/202212/t20221216_5885213.html)，并保留其历史日期。

官方库存收录 10 个历史纳凉场所：图书馆、蓝海城市书房、西街社区城市图书驿站及 7 个爱心驿站。本轮已用公开百度机构点定位其中的都昌县图书馆，并关联 `official-cooling-county-library`；其余 9 处仍待定位。不能用所处广场的中心替代驿站。2024/2025 新闻只支持当年开放事实；本版地图没有把它们标成 2026 当前开放避暑点。

县图书馆的百度点距 OSM 文化中心 library 建筑质心约 83 米。两条来源暂保留为候选地图点，可能同属一处文化设施群；**不能据此声称有两处独立纳凉场所**。图书馆的2025年报道仅记在历史证据字段，当前空调、可进入性和开放时间仍为未知。

## 暂未发布或待核验

| 记录 | 公开线索 | 本版处理 |
| --- | --- | --- |
| 鸣山乡卫生院 | 官方 2026 机构表及 2022 马涧桥街地址；普通地图检索未取得可信同名设施点 | 保留官方待定位记录，无替代坐标 |
| 汪墩中心卫生院 | 百度 UID `e116be542d92117bc91b9565`，地址 210 省道；Google 有相距数公里的两条同名机构位置 | 院址、分院或迁址关系待核对，不发布该点 |
| 汪墩乡中心卫生院-发热门诊 | 百度 UID `402f9e06c0c75e5017948e6a`，地址写喆桥集镇，但坐标与上条约 9 米；不能仅凭文字认定已消除冲突 | 不发布、不单独算一所医院 |
| 北山商城分院、成爱香卫生所、七角村卫生所、七角医院等 Google 条目 | 名称和地址可读，但本轮未证实大陆 Google 条目坐标 datum；部分同名点/边界归属有差异 | 不猜转换、不作为可用 WGS84 点 |
| POI86 二手目录 | 公开页虽列“大地/火星”坐标，但[声明](https://www.poi86.com/page/8.html)包含非商业、24 小时删除等条款且权利链不明 | 未复制进本项目点位数据；仅作检索线索 |

## 发布点逐项索引

| 地图点原名 | 类型 | 几何归属乡镇 | 官方关联 ID | 原始来源 |
| --- | --- | --- | --- | --- |
| 文化中心 | library | 都昌镇 | 未关联 | [osm:way/618453861](https://www.openstreetmap.org/way/618453861) |
| 万户卫生院 | health_centre | 万户镇 | official-medical-H36042800836 | [baidu-public:a2d113fe0d16c33be6e9656f](https://map.baidu.com/poi/%E4%B8%87%E6%88%B7%E5%8D%AB%E7%94%9F%E9%99%A2/?uid=a2d113fe0d16c33be6e9656f) |
| 三汊港卫生院 | health_centre | 三汊港镇 | official-medical-H36042800839 | [baidu-public:cdef4edf6dc41d4e28d7ab65](https://map.baidu.com/poi/%E4%B8%89%E6%B1%8A%E6%B8%AF%E5%8D%AB%E7%94%9F%E9%99%A2/?uid=cdef4edf6dc41d4e28d7ab65) |
| 中馆卫生院 | health_centre | 中馆镇 | official-medical-H36042800852 | [baidu-public:53349ee3b9c4f56e22026264](https://map.baidu.com/poi/%E4%B8%AD%E9%A6%86%E5%8D%AB%E7%94%9F%E9%99%A2/?uid=53349ee3b9c4f56e22026264) |
| 北山乡卫生院-预防接种门诊 | vaccination_clinic | 北山乡 | 未关联 | [baidu-public:2b5fa11401378a50f02d8382](https://map.baidu.com/poi/%E5%8C%97%E5%B1%B1%E4%B9%A1%E5%8D%AB%E7%94%9F%E9%99%A2-%E9%A2%84%E9%98%B2%E6%8E%A5%E7%A7%8D%E9%97%A8%E8%AF%8A/?uid=2b5fa11401378a50f02d8382) |
| 南峰卫生院 | health_centre | 南峰镇 | official-medical-H36042800838 | [baidu-public:a5097bfa0b806063f03cba65](https://map.baidu.com/poi/%E5%8D%97%E5%B3%B0%E5%8D%AB%E7%94%9F%E9%99%A2/?uid=a5097bfa0b806063f03cba65) |
| 周溪卫生院 | health_centre | 周溪镇 | official-medical-H36042800670 | [baidu-public:35debf293378e844a6da396a](https://map.baidu.com/poi/%E5%91%A8%E6%BA%AA%E5%8D%AB%E7%94%9F%E9%99%A2/?uid=35debf293378e844a6da396a) |
| 和合乡卫生院 | health_centre | 和合乡 | official-medical-H36042800837 | [baidu-public:750ec165da81b6554c3a591a](https://map.baidu.com/poi/%E5%92%8C%E5%90%88%E4%B9%A1%E5%8D%AB%E7%94%9F%E9%99%A2/?uid=750ec165da81b6554c3a591a) |
| 土塘卫生院 | health_centre | 土塘镇 | official-medical-H36042800668 | [baidu-public:49d959384c879743c61e5164](https://map.baidu.com/poi/%E5%9C%9F%E5%A1%98%E5%8D%AB%E7%94%9F%E9%99%A2/?uid=49d959384c879743c61e5164) |
| 多宝卫生院 | health_centre | 多宝乡 | official-medical-H36042800840 | [baidu-public:3dc9befdbfee4016bea24064](https://map.baidu.com/poi/%E5%A4%9A%E5%AE%9D%E5%8D%AB%E7%94%9F%E9%99%A2/?uid=3dc9befdbfee4016bea24064) |
| 都昌县多宝乡寺前村卫生院 | clinic | 多宝乡 | 未关联 | [baidu-public:28fb83afdbde55044251c0a7](https://map.baidu.com/poi/%E9%83%BD%E6%98%8C%E5%8E%BF%E5%A4%9A%E5%AE%9D%E4%B9%A1%E5%AF%BA%E5%89%8D%E6%9D%91%E5%8D%AB%E7%94%9F%E9%99%A2/?uid=28fb83afdbde55044251c0a7) |
| 大树卫生院 | health_centre | 大树乡 | official-medical-H36042800813 | [baidu-public:be54cde93dbcc75878f79665](https://map.baidu.com/poi/%E5%A4%A7%E6%A0%91%E5%8D%AB%E7%94%9F%E9%99%A2/?uid=be54cde93dbcc75878f79665) |
| 都昌县中医院 | hospital | 大树乡 | official-medical-H36042800635 | [tencent-public:4749403447891313513](https://map.qq.com/poi/?sm=4749403447891313513&keyfrom=1) |
| 大沙镇卫生院 | health_centre | 大沙镇 | official-medical-H36042800846 | [baidu-public:cde9e2c7ea9f76b4e81e9765](https://map.baidu.com/poi/%E5%A4%A7%E6%B2%99%E9%95%87%E5%8D%AB%E7%94%9F%E9%99%A2/?uid=cde9e2c7ea9f76b4e81e9765) |
| 都昌县大港镇卫生院 | health_centre | 大港镇 | official-medical-H36042800667 | [baidu-public:30f875a0ec21e0db45e27466](https://map.baidu.com/poi/%E9%83%BD%E6%98%8C%E5%8E%BF%E5%A4%A7%E6%B8%AF%E9%95%87%E5%8D%AB%E7%94%9F%E9%99%A2/?uid=30f875a0ec21e0db45e27466) |
| 左里卫生院 | health_centre | 左里镇 | official-medical-H36042800841 | [baidu-public:d146b388cd8193de74026964](https://map.baidu.com/poi/%E5%B7%A6%E9%87%8C%E5%8D%AB%E7%94%9F%E9%99%A2/?uid=d146b388cd8193de74026964) |
| 徐埠中心卫生院 | health_centre | 徐埠镇 | official-medical-H36042800662 | [baidu-public:08f1f1f5f0b90fca88db7364](https://map.baidu.com/poi/%E5%BE%90%E5%9F%A0%E4%B8%AD%E5%BF%83%E5%8D%AB%E7%94%9F%E9%99%A2/?uid=08f1f1f5f0b90fca88db7364) |
| 春桥卫生院 | health_centre | 春桥乡 | official-medical-H36042800665 | [baidu-public:05dd7d5f6c768b01ad080f64](https://map.baidu.com/poi/%E6%98%A5%E6%A1%A5%E5%8D%AB%E7%94%9F%E9%99%A2/?uid=05dd7d5f6c768b01ad080f64) |
| 都昌县狮山医院 | hospital | 狮山乡 | 未关联 | [baidu-public:4edf9fbf1f892694cc06a465](https://map.baidu.com/poi/%E9%83%BD%E6%98%8C%E5%8E%BF%E7%8B%AE%E5%B1%B1%E5%8C%BB%E9%99%A2/?uid=4edf9fbf1f892694cc06a465) |
| 都昌县芗溪乡卫生院 | health_centre | 芗溪乡 | official-medical-H36042800664 | [baidu-public:7c1699ef85046272c6f02583](https://map.baidu.com/poi/%E9%83%BD%E6%98%8C%E5%8E%BF%E8%8A%97%E6%BA%AA%E4%B9%A1%E5%8D%AB%E7%94%9F%E9%99%A2/?uid=7c1699ef85046272c6f02583) |
| 苏山乡卫生院 | health_centre | 苏山乡 | official-medical-H36042800842 | [baidu-public:f9fd6062bb81bd1f0f0e6b64](https://map.baidu.com/poi/%E8%8B%8F%E5%B1%B1%E4%B9%A1%E5%8D%AB%E7%94%9F%E9%99%A2/?uid=f9fd6062bb81bd1f0f0e6b64) |
| 都昌县蔡岭中心卫生院 | health_centre | 蔡岭镇 | official-medical-H36042800669 | [osm:way/1485918607](https://www.openstreetmap.org/way/1485918607) |
| 西源卫生院 | health_centre | 西源乡 | official-medical-H36042800666 | [baidu-public:9f97b1c531ae2107b64f5459](https://map.baidu.com/poi/%E8%A5%BF%E6%BA%90%E5%8D%AB%E7%94%9F%E9%99%A2/?uid=9f97b1c531ae2107b64f5459) |
| 都昌县人民医院 | hospital | 都昌镇 | official-medical-H36042800622 | [amap-public:B03180099C](https://ditu.amap.com/place/B03180099C) |
| 都昌镇卫生院 | health_centre | 都昌镇 | official-medical-H36042800956 | [baidu-public:92b9f9b856a09834d13ce3a1](https://map.baidu.com/poi/%E9%83%BD%E6%98%8C%E9%95%87%E5%8D%AB%E7%94%9F%E9%99%A2/?uid=92b9f9b856a09834d13ce3a1) |
| 阳峰乡卫生院-发热门诊 | fever_clinic | 阳峰乡 | 未关联 | [baidu-public:17f2add11de2d0f990114c1d](https://map.baidu.com/poi/%E9%98%B3%E5%B3%B0%E4%B9%A1%E5%8D%AB%E7%94%9F%E9%99%A2-%E5%8F%91%E7%83%AD%E9%97%A8%E8%AF%8A/?uid=17f2add11de2d0f990114c1d) |
| 都昌县图书馆 | library | 都昌镇 | official-cooling-county-library | [baidu-public:f4351cb56a957914b05c6aba](https://map.baidu.com/poi/都昌县图书馆/?uid=f4351cb56a957914b05c6aba) |

## 复核与原始证据

本轮检查：所有发布点均为有限 WGS84 坐标并落入县域乡镇几何；feature ID 唯一；21 个医疗及 1 个图书馆官方 ID 均存在且唯一关联；科室没有主院关联；所有空调、公共可进入及当前开放字段均未知；0 个 `cooling_verified`。原始 OSM XML、百度 26 条医疗 MC 记录及 1 条县图书馆 MC 记录和分步转换、跨源检查另存本地研究归档，不作运行依赖。

主要文件：`baidu-records.json`、`baidu-records-converted.json`、`conversion-checks.json`、`resources-excluded-and-checks.json`、`osm-county-map-0.xml`、`osm-county-map-3.xml`、`library-final-raw.json`、`library-final-converted.json`。每个发布点同时将必要的原坐标、对象 ID 和来源保存在 GeoJSON，避免只依赖临时文件。数据许可按各来源逐项保留，与项目代码许可分开。

归档证据 SHA-256（用于匹配本地研究归档）：

| 文件 | SHA-256 |
| --- | --- |
| `baidu-records.json` | `1f101b701f02d096a78d365e651f333c72e9ed1752b6e110b427d9975f09ff32` |
| `baidu-records-converted.json` | `cd3a8297fd7691c4238f68e1fc63f925600d47052bc5c1d61844eb6b2a1bd435` |
| `library-final-raw.json` | `57bf1e48cc8a19ce76b760a379c417ff7753080f054146eb374f4f9cfa4ce354` |
| `library-final-converted.json` | `a812a9ca78b875708efbf932e6c53ddc6b99abde88564754c5e6ff5689a93594` |
| `conversion-checks.json` | `62f70c88caf07d8768b48c749eb07ab81bbc6e802520b64ef921f04637f9c246` |
