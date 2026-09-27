#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线构建都昌县公开聚落点：输入原件，保留来源，不推测行政级别或人口。

仅使用Python标准库。支持Overpass原始JSON（可含_retrieval元信息）、
GeoNames记录JSON快照或CN.zip/CN.txt；不会联网或写入原始文件。
"""
import argparse
import collections
import hashlib
import json
import math
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GEONAMES_FIELDS = (
    "geonameid", "name", "asciiname", "alternatenames", "latitude", "longitude",
    "feature_class", "feature_code", "country_code", "cc2", "admin1_code",
    "admin2_code", "admin3_code", "admin4_code", "population", "elevation",
    "dem", "timezone", "modification_date",
)

def ring(x,y,points):
    inside=False
    for (ax,ay),(bx,by) in zip(points,points[1:]+points[:1]):
        cross=(x-ax)*(by-ay)-(y-ay)*(bx-ax)
        if abs(cross)<=1e-12 and min(ax,bx)-1e-10<=x<=max(ax,bx)+1e-10 and min(ay,by)-1e-10<=y<=max(ay,by)+1e-10:return 0
        if (ay>y)!=(by>y) and x<(bx-ax)*(y-ay)/(by-ay)+ax:inside=not inside
    return 1 if inside else -1

def covers(x,y,g):
    return any(ring(x,y,p[0])>=0 and not any(ring(x,y,h)>0 for h in p[1:]) for p in ([g['coordinates']] if g['type']=='Polygon' else g['coordinates']))

def township(x,y,towns):
    matches=[f['properties']['name_zh'] for f in towns if covers(x,y,f['geometry'])]
    return matches[0] if len(matches)==1 else None,matches

def normalize(name):return re.sub(r'村$', '',name).strip().lower()
def km(a,b):
    x1,y1,x2,y2=map(math.radians,(a['lon'],a['lat'],b['lon'],b['lat']))
    h=math.sin((y2-y1)/2)**2+math.cos(y1)*math.cos(y2)*math.sin((x2-x1)/2)**2
    return 12742.0176*math.asin(min(1,math.sqrt(h)))


def build_collection(osm, gn, towns, county, boundary_sha, built_at):
    records=[];rejected=[]
    def add(record):
        town,matches=township(record['lon'],record['lat'],towns)
        if not town:
            rejected.append({**record,'reason':'county_boundary_gap' if covers(record['lon'],record['lat'],county) else 'outside_study_boundary','township_matches':matches})
            return
        record['township_name']=town
        records.append(record)
    for e in osm['elements']:
        t=e.get('tags',{});name=t.get('name:zh') or t.get('name');place=t.get('place')
        base={'id':f"osm-node-{e['id']}",'source_id':str(e['id']),'source_type':'osm','source_url':f"https://www.openstreetmap.org/node/{e['id']}",'lon':e['lon'],'lat':e['lat'],'tags':t}
        if not name or place not in ('village','hamlet') or t.get('capital') or t.get('place:CN') in ('township','town'):
            rejected.append({**base,'reason':'unnamed_or_not_village_or_township_seat'});continue
        aliases=[]
        for k in ('name','name:zh','name:en','alt_name','old_name'):aliases.extend(t.get(k,'').split(';'))
        add({**base,'name':name,'aliases':sorted(set(a for a in aliases if a)), 'source_updated_at':e.get('timestamp'),
          'retrieved_at':osm['_retrieval']['retrieved_at_utc'],'coordinate_source':'OpenStreetMap 原始聚落节点（WGS84）',
          'coordinate_precision':'mapped_point','settlement_level':'unknown','source_feature_type':place,'license':'ODbL 1.0; © OpenStreetMap contributors'})
    for e in gn['records']:
        if e['feature_class']!='P':continue
        base={'id':f"geonames-{e['geonameid']}",'source_id':str(e['geonameid']),'source_type':'geonames','source_url':f"https://www.geonames.org/{e['geonameid']}/",'lon':float(e['longitude']),'lat':float(e['latitude'])}
        if e['feature_code']!='PPL':rejected.append({**base,'reason':'geonames_not_PPL','feature_code':e['feature_code']});continue
        aliases=sorted(set([e['name'],e['asciiname']]+e['alternatenames'].split(',')));zh=[a for a in aliases if re.search('[\u3400-\u9fff]',a)]
        name=next((a for a in zh if a.endswith('村')),zh[0] if zh else e['name'])
        add({**base,'name':name,'aliases':aliases,'source_updated_at':e['modification_date'],'retrieved_at':gn['retrieved_at_utc'],
          'coordinate_source':'GeoNames PPL 聚落地名点（WGS84；可能为旧位置或近似中心）','coordinate_precision':'approximate','settlement_level':'unknown',
          'source_feature_type':e['feature_code'],'license':'CC BY 4.0; GeoNames'})
    # 同乡、同名（或别名）且不超过150米才允许并成一点；合并仍保留每条来源原坐标。
    # 限制簇内所有点两两接近，避免传递合并把两个相距过远的村连成一个点。
    records.sort(key=lambda r:(r['source_type']!='osm',r['id']))
    clusters=[]
    for r in records:
        names={normalize(a) for a in r['aliases'] if a}
        choices=[]
        for i,c in enumerate(clusters):
            if c[0]['township_name']!=r['township_name']:continue
            if all(names & {normalize(a) for a in member['aliases'] if a} and km(r,member)<=0.15 for member in c):choices.append(i)
        if len(choices)==1:clusters[choices[0]].append(r)
        else:clusters.append([r])
    features=[]
    for cluster in clusters:
        first=cluster[0]
        sources=[{'id':r['id'],'source_id':r['source_id'],'source_type':r['source_type'],'name':r['name'],'url':r['source_url'],'license':r['license'],
           'source_updated_at':r['source_updated_at'],'retrieved_at':r['retrieved_at'],'coordinate_source':r['coordinate_source'],
           'coordinate_precision':r['coordinate_precision'],'coordinates':[r['lon'],r['lat']],'source_feature_type':r['source_feature_type']} for r in cluster]
        p={k:first[k] for k in ('id','name','township_name','source_url','source_id','source_type','source_updated_at','retrieved_at','coordinate_source','coordinate_precision','settlement_level')}
        p.update(kind='settlement',verification_status='publicly_listed',population=None,elderly_ratio=None,
          aliases=sorted(set(a for r in cluster for a in r['aliases'] if a and a!=first['name'])),sources=sources,potential_duplicate=False,potential_duplicate_ids=[],
          township_assignment='OSM乡界多边形空间归属；并非法定隶属核验',deduplication_note='同乡同名/别名且簇内两两相距不超过150米合并；优先OSM原始点' if len(cluster)>1 else None)
        features.append({'type':'Feature','id':first['id'],'geometry':{'type':'Point','coordinates':[first['lon'],first['lat']]},'properties':p})
    # 超过去重阈值的跨源同名记录保留，并显式标示待人工核对，不能擅自选坐标。
    for i,a in enumerate(features):
        pa=a['properties'];na={normalize(x) for x in [pa['name']]+pa['aliases']};sa={s['source_type'] for s in pa['sources']}
        for b in features[i+1:]:
            pb=b['properties'];sb={s['source_type'] for s in pb['sources']}
            if pa['township_name']!=pb['township_name'] or not (('osm' in sa and 'geonames' in sb) or ('geonames' in sa and 'osm' in sb)):continue
            if na & {normalize(x) for x in [pb['name']]+pb['aliases']}:
                pa['potential_duplicate']=pb['potential_duplicate']=True;pa['potential_duplicate_ids'].append(pb['id']);pb['potential_duplicate_ids'].append(pa['id'])
    features.sort(key=lambda f:(f['properties']['township_name'],f['properties']['name'],f['id']))
    counts=collections.Counter(f['properties']['township_name'] for f in features)
    rawcounts={t:{s:sum(r['township_name']==t and r['source_type']==s for r in records) for s in ('osm','geonames')} for t in sorted(counts)}
    collection={'type':'FeatureCollection','schema_version':'1.0','retrieved_at':built_at,
     'coverage_note':'覆盖都昌县24乡镇的公开聚落地名候选点，非完整行政村/自然村名录。OSM与GeoNames条目保留来源及更新日期；下载日期不表示实地核验或当年更新。乡镇数量不是官方村覆盖率。自然/行政村级别、人口及老年比例未经核验，不补猜。',
     'sources':[{'id':'osm','name':'OpenStreetMap contributors','url':'https://www.openstreetmap.org/copyright','license':'ODbL 1.0'}, {'id':'geonames','name':'GeoNames','url':'https://download.geonames.org/export/dump/readme.txt','license':'CC BY 4.0'}],
     'license_note':'本合并派生数据库按ODbL 1.0保留OpenStreetMap署名；同时保留GeoNames CC BY 4.0署名和原始来源。',
     'features':features}
    summary={'feature_count':len(features),'raw_assigned_records':len(records),'merged_record_count':len(records)-len(features),'potential_duplicate_features':sum(f['properties']['potential_duplicate'] for f in features),'township_count':len(counts),'counts':dict(counts),'raw_counts':rawcounts,'excluded_counts':dict(collections.Counter(r['reason'] for r in rejected))}
    lines=['# 都昌县公开聚落点：来源、筛选与质量边界','',f'生成/检索记录：{built_at}','',f"本次共 {len(features)} 个聚落候选点，空间分布覆盖 {len(counts)} 个乡镇；这不是官方村庄总数，也不是完整覆盖率。",'', '## 数据与许可','',
     '- OpenStreetMap：只使用具名 `place=village` / `place=hamlet` 原始节点，WGS84，标为 `mapped_point`；该标签反映聚落，不直接证明自然村或行政村级别；mapped_point仅表示原始地图点，不表示GPS精度或现场核验。保留 OSM 对象链接和最后编辑时间。© OpenStreetMap contributors，ODbL 1.0。',
     '- GeoNames：中国公开地名库 `CN.zip`，只使用 `PPL`（populated place）记录，WGS84，标为 `approximate`。明确排除 `PPLA*` 驻地、`PPLF` 农场等；不少条目最后更新于2021或2023年。GeoNames，CC BY 4.0。',
     '- 下载/检索日期与每条记录的最后更新时间分别保存；下载于2026年并不意味着地名、位置已在2026年复核。两源可能共同来自GNS或互相引用，不当作两个独立实测证据。',
     '- GeoNames不保证准确性、及时性、完整性；OSM是社区地图。名称含“村”也不足以确认行政级别，所有 `settlement_level` 保持 `unknown`，人口、老年比例保持 `null`。',
     '- 合并派生数据库保留ODbL 1.0与OpenStreetMap署名，同时保留GeoNames CC BY 4.0署名；逐点 `sources` 可追溯贡献来源。','',
     '公开来源：[OSM标签说明](https://wiki.openstreetmap.org/wiki/Key:place)、[OSM许可](https://www.openstreetmap.org/copyright)、[GeoNames完整字段/许可说明](https://download.geonames.org/export/dump/readme.txt)、[GeoNames类型](https://www.geonames.org/export/codes.html)、[本次下载](https://download.geonames.org/export/dump/CN.zip)。','',
     '## 获取与空间筛选','',f"- OSM端点：`{osm['_retrieval']['endpoint']}`；OSM数据基准时间 `{osm.get('osm3s',{}).get('timestamp_osm_base')}`。",f"- 查询：`{osm['_retrieval']['query']}`。只查询节点，不把村级边界的包围盒中心伪装为村庄坐标。",
     f"- GeoNames文件HTTP Last-Modified：`{gn.get('http_last_modified')}`；SHA-256：`{gn.get('download_sha256')}`。",
     f"- 乡界：仓库 `data/gis/duchang_townships_osm.geojson`，SHA-256 `{boundary_sha}`，2026-09-26来源快照。",
     '- 只集成唯一落入上述24乡界的点。县外包框中的县外记录排除；乡界缝隙、交叠或边界归属不能唯一判断的记录保留在研究排除表中，未按最近乡驻地猜配。乡界及村点都不作为法定行政归属证明。',
     '- 未引入项目原先人工维护的GCJ-02村点，避免把维护数据误标为公共来源；所有本文件坐标直接来自WGS84公开源。','',
     '## 去重与残余疑点','',f"- 原始唯一归属记录 {len(records)} 条，合并减少 {len(records)-len(features)} 条；同乡、同名或别名（仅去末尾“村”并统一拉丁大小写）、簇内所有坐标两两不超过150米才合并。",
     '- 合并优先保留OSM原始点坐标；每条来源原来的名称、坐标、更新时间都保存在 `properties.sources`，不平均坐标。',
     f"- 超过去重阈值的跨源同乡同名点继续分别保留，{summary['potential_duplicate_features']}个点标记 `potential_duplicate=true` 并列出关联ID；可能是坐标误差、行政村/自然村同名或两个不同聚落，需人工核查。",
     '- 不依据聚落规模标签猜行政/自然村，不生成未出现于来源中的名称和坐标。未经村委或实地核验，本文件用于辅助地图与候选检索。','',
     '## 乡镇分布（仅本数据集点数）','','| 乡镇 | 原始OSM | 原始GeoNames PPL | 去重后候选点 |','|---|---:|---:|---:|']
    for t in sorted(counts):lines.append(f"| {t} | {rawcounts[t]['osm']} | {rawcounts[t]['geonames']} | {counts[t]} |")
    lines += ['', '## 可复核原件','', '原始文件另存本地研究归档，不作运行依赖；原件文件名与指纹用于对应本轮快照：',
     '- `osm_duchang_bbox_settlements_raw.json`：Overpass原始节点、原始标签、查询和检索时间。','- `geonames_CN.zip`：官方整包，与上面SHA-256对应。','- `geonames_duchang_bbox_raw.json`：县外包框内原始字段。','- `county_settlements_excluded.json`：无名、非村类型、县外和边界待核验排除记录。','- `county_settlements_summary.json`：原始来源数量、去重和乡镇分布。','- `scripts/gis/build_public_settlements.py`：仓库离线维护脚本，显式输入原始文件路径；默认无联网且不写原件。','',f"排除原因汇总：`{json.dumps(summary['excluded_counts'],ensure_ascii=False)}`。",'']
    lines += ['', '## 离线复建', '', '从研究归档取得原始输入后执行（路径为示例占位符）：', '', '```bash', 'python scripts/gis/build_public_settlements.py \\', '  --osm /path/to/osm_duchang_bbox_settlements_raw.json \\', '  --geonames /path/to/geonames_duchang_bbox_raw.json \\', '  --output /path/to/duchang_settlements.geojson \\', '  --sources-output /path/to/duchang_settlements_sources.md', '```', '', '也支持直接读取GeoNames CN.zip或CN.txt，此时必须用 --retrieved-at 明确原始下载时间。脚本只离线读取输入；不会自行请求API、更新原件或覆盖输入。--townships/--county-cells 可指定边界快照；--report-dir 可输出排除记录与分布统计。', '']
    return collection, '\n'.join(lines), summary, rejected

def _bounds(towns):
    points = [point for feature in towns
              for polygon in ([feature["geometry"]["coordinates"]]
                              if feature["geometry"]["type"] == "Polygon"
                              else feature["geometry"]["coordinates"])
              for ring_points in polygon for point in ring_points]
    return min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)


def _retrieval_time(value):
    if not value:
        raise ValueError("原始文件没有检索时间，请使用 --retrieved-at 指定原始下载时间")
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("--retrieved-at 必须包含时区，例如 2026-09-27T04:00:00Z")
    return str(value)


def load_osm(path, retrieved_at):
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
        raise ValueError("OSM输入必须为包含elements数组的Overpass JSON")
    if payload.get("remark"):
        raise ValueError("Overpass返回含remark错误，不能发布可能不完整的数据")
    metadata = payload.setdefault("_retrieval", {})
    metadata["retrieved_at_utc"] = _retrieval_time(metadata.get("retrieved_at_utc") or retrieved_at)
    metadata.setdefault("endpoint", "原始输入未记录端点")
    metadata.setdefault("query", "原始输入未记录查询；仅使用该文件内具名village/hamlet节点")
    # 外部原件可能同时包含关系或面，不将其中心猜成村庄点。
    payload["elements"] = [entry for entry in payload["elements"] if entry.get("type") == "node"]
    return payload


def load_geonames(path, bounds, retrieved_at):
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
            raise ValueError("GeoNames JSON必须包含records数组和原始字段")
        payload["retrieved_at_utc"] = _retrieval_time(payload.get("retrieved_at_utc") or retrieved_at)
        return payload
    timestamp = _retrieval_time(retrieved_at)
    rows = []
    west, south, east, north = bounds
    def consume(lines):
        for raw_line in lines:
            line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
            values = line.rstrip("\n\r").split("\t")
            if len(values) != len(GEONAMES_FIELDS):
                raise ValueError("GeoNames行字段数不符合官方19字段格式")
            lat, lon = float(values[4]), float(values[5])
            if west <= lon <= east and south <= lat <= north:
                rows.append(dict(zip(GEONAMES_FIELDS, values)))
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            if "CN.txt" not in archive.namelist():
                raise ValueError("GeoNames ZIP缺少CN.txt")
            with archive.open("CN.txt") as lines:
                consume(lines)
    else:
        with path.open(encoding="utf-8") as lines:
            consume(lines)
    return {"source": "https://download.geonames.org/export/dump/CN.zip",
            "retrieved_at_utc": timestamp, "records": rows,
            "download_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "http_last_modified": None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--osm", type=Path, required=True, help="Overpass原始JSON路径")
    parser.add_argument("--geonames", type=Path, required=True, help="GeoNames JSON、CN.zip或CN.txt路径")
    parser.add_argument("--townships", type=Path, default=PROJECT_ROOT / "data/gis/duchang_townships_osm.geojson")
    parser.add_argument("--county-cells", type=Path, default=PROJECT_ROOT / "data/gis/duchang_heat_exposure_cells.geojson")
    parser.add_argument("--output", type=Path, required=True, help="输出GeoJSON路径")
    parser.add_argument("--sources-output", type=Path, help="来源说明路径；默认与GeoJSON同目录，以_sources.md结尾")
    parser.add_argument("--report-dir", type=Path, help="可选排除记录及统计输出目录")
    parser.add_argument("--retrieved-at", help="输入缺少元信息时明确其原始检索时间（含时区）")
    args = parser.parse_args(argv)
    sources_output = args.sources_output or args.output.with_name(args.output.stem + "_sources.md")
    outputs = [args.output, sources_output]
    if args.report_dir:
        outputs += [args.report_dir / "county_settlements_summary.json", args.report_dir / "county_settlements_excluded.json"]
    inputs = {path.resolve() for path in (args.osm, args.geonames, args.townships, args.county_cells)}
    if len({path.resolve() for path in outputs}) != len(outputs) or any(path.resolve() in inputs for path in outputs):
        parser.error("输出不能相互重叠或覆盖任何原始输入")
    try:
        towns = json.loads(args.townships.read_text(encoding="utf-8"))["features"]
        cells = json.loads(args.county_cells.read_text(encoding="utf-8"))["features"]
        county = next(feature["geometry"] for feature in cells if feature["properties"].get("feature_type") == "study_boundary")
        osm = load_osm(args.osm, args.retrieved_at)
        geonames = load_geonames(args.geonames, _bounds(towns), args.retrieved_at)
        built_at = datetime.now(timezone.utc).isoformat()
        boundary_sha = hashlib.sha256(args.townships.read_bytes()).hexdigest()
        collection, notes, summary, rejected = build_collection(osm, geonames, towns, county, boundary_sha, built_at)
    except (OSError, ValueError, KeyError, StopIteration, zipfile.BadZipFile) as exc:
        parser.error(str(exc))
    notes += "\n## 本次输入原件指纹\n\n"
    for input_path in (args.osm, args.geonames, args.townships, args.county_cells):
        notes += f"- `{input_path.name}`：SHA-256 `{hashlib.sha256(input_path.read_bytes()).hexdigest()}`。\n"
    for path in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(collection, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8")
    sources_output.write_text(notes, encoding="utf-8")
    if args.report_dir:
        outputs[2].write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        outputs[3].write_text(json.dumps(rejected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
