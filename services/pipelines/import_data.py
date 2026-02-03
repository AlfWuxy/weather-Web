# -*- coding: utf-8 -*-
"""
数据导入脚本 - 将Excel数据导入数据库
"""
from collections import Counter
from datetime import datetime
from pathlib import Path
import random

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_PATH = ROOT_DIR / 'data' / 'research' / '数据.xlsx'

from core.app import create_app
from core.db_models import MedicalRecord, Community, WeatherData
from core.extensions import db
from core.time_utils import local_datetime_to_utc, today_local

app = create_app(register_blueprints=False)

def get_surname_communities_mapping():
    """获取姓氏到社区列表的映射关系。

    - 仅当一个姓氏天然分布在多个村庄时，返回多个社区名称
    - 其他姓氏只会分配到一个对应社区
    """
    mapping = {
        '周': ['牛家垄周村'],
        '徐': ['岭背徐村', '徐家湾', '徐家咀', '竹峦徐村', '樟树湾徐村'],  # 徐家湾与岭背徐村相连，樟树湾徐村又称细屋徐村
        '谭': ['谭家新村'],
        '汪': ['新屋汪家', '新舍汪家'],
        '段': ['段家颈村'],
        '吴': ['吴家仓', '庙北吴村'],
        '邵': ['茅棚邵村', '鲶鱼山邵村'],
        '伍': ['伍家湾村'],
        '付': ['上下付村']
    }
    return mapping

def get_community_detail_info():
    """获取社区详细信息（位置描述）"""
    return {
        '牛家垄周村': {
            'location': '牛家垄周村',
            'description': '周姓主要居住地'
        },
        '岭背徐村': {
            'location': '岭背徐村',
            'description': '徐姓主要居住地（与徐家湾在一起）'
        },
        '徐家湾': {
            'location': '徐家湾',
            'description': '徐姓主要居住地（与岭背徐村在一起）'
        },
        '徐家咀': {
            'location': '徐家咀',
            'description': '徐姓主要居住地'
        },
        '竹峦徐村': {
            'location': '竹峦徐村',
            'description': '徐姓主要居住地（与樟树湾徐村连着）'
        },
        '樟树湾徐村': {
            'location': '樟树湾徐村（细屋徐村）',
            'description': '徐姓主要居住地（与竹峦徐村连着，也叫细屋徐村）'
        },
        '谭家新村': {
            'location': '谭家新村',
            'description': '谭姓主要居住地'
        },
        '新屋汪家': {
            'location': '新屋汪家',
            'description': '汪姓主要居住地'
        },
        '新舍汪家': {
            'location': '新舍汪家',
            'description': '汪姓主要居住地'
        },
        '段家颈村': {
            'location': '段家颈村',
            'description': '段姓主要居住地'
        },
        '吴家仓': {
            'location': '吴家仓',
            'description': '吴姓主要居住地'
        },
        '庙北吴村': {
            'location': '庙北吴村',
            'description': '吴姓主要居住地'
        },
        '茅棚邵村': {
            'location': '茅棚邵村',
            'description': '邵姓主要居住地'
        },
        '鲶鱼山邵村': {
            'location': '鲶鱼山邵村',
            'description': '邵姓主要居住地（主要人口）'
        },
        '伍家湾村': {
            'location': '伍家湾村',
            'description': '伍姓主要居住地'
        },
        '上下付村': {
            'location': '上下付村',
            'description': '付姓主要居住地'
        }
    }

def scan_surnames_from_data():
    """扫描Excel数据，自动识别所有姓氏并生成完整的社区映射"""
    print("正在扫描数据，识别姓氏...")
    
    df = pd.read_excel(DATA_PATH, header=None)
    df.columns = [
        '序号', '医保', '姓名', '性别', '年龄', '就诊时间', 
        '科室', '医生', '疾病分类', '主诉', '病历描述', 
        '列11', '体温', '心率', '血压'
    ]
    
    # 统计所有姓氏
    surnames = []
    for index, row in df.iterrows():
        patient_name = str(row['姓名'])
        if patient_name and len(patient_name) > 0 and patient_name != 'nan':
            surname = patient_name[0]
            if surname and surname.strip():
                surnames.append(surname)
    
    surname_count = Counter(surnames)
    
    print(f"识别到 {len(surname_count)} 个姓氏")
    print("姓氏统计（前20名）：")
    for surname, count in surname_count.most_common(20):
        percentage = (count / len(surnames) * 100) if surnames else 0
        print(f"  {surname}姓：{count}人 ({percentage:.1f}%)")
    
    # 获取已有的实际村庄映射（一个姓氏对应多个社区）
    existing_mapping = get_surname_communities_mapping()
    
    # 统计所有实际社区名称
    all_actual_communities = []
    for communities_list in existing_mapping.values():
        all_actual_communities.extend(communities_list)
    
    # 生成完整的姓氏到社区列表的映射（用于随机分配）
    surname_communities_map = {}
    unspecified_surnames = []  # 记录未指定村庄的姓氏
    
    for surname in surname_count.keys():
        if surname in existing_mapping:
            # 使用实际村庄列表（多个独立社区）
            surname_communities_map[surname] = existing_mapping[surname]
        else:
            # 未指定村庄的姓氏，随机分配到16个真实村庄中
            surname_communities_map[surname] = all_actual_communities  # 分配到所有真实村庄
            unspecified_surnames.append(surname)
    
    print(f"\n社区映射统计：")
    existing_count = len([s for s in surname_communities_map.keys() if s in existing_mapping])
    new_count = len(unspecified_surnames)
    print(f"  已指定村庄：{existing_count} 个姓氏（对应 {len(all_actual_communities)} 个独立社区）")
    print(f"  未指定村庄：{new_count} 个姓氏 -> 随机分配到16个真实村庄")
    if unspecified_surnames:
        print(f"  未指定的姓氏：{'、'.join(unspecified_surnames)}")
    
    return surname_communities_map

def import_medical_records(surname_community_map=None):
    """导入病历数据 - 根据姓氏自动分配到社区"""
    print("正在读取Excel文件...")
    
    # 读取Excel，第一行是数据不是标题
    df = pd.read_excel(DATA_PATH, header=None)
    
    # 定义列名（基于截图）
    df.columns = [
        '序号', '医保', '姓名', '性别', '年龄', '就诊时间', 
        '科室', '医生', '疾病分类', '主诉', '病历描述', 
        '列11', '体温', '心率', '血压'
    ]
    
    print(f"共读取 {len(df)} 条记录")
    
    with app.app_context():
        # 清空现有数据
        MedicalRecord.query.delete()
        
        # 如果没有提供姓氏映射，先扫描数据获取
        if surname_community_map is None:
            surname_community_map = scan_surnames_from_data()
        
        # 导入数据
        success_count = 0
        for index, row in df.iterrows():
            try:
                # 处理年龄（去除"岁"）
                age_str = str(row['年龄']).replace('岁', '').strip()
                try:
                    age = int(age_str)
                except (ValueError, TypeError):
                    age = None
                
                # 处理就诊时间
                visit_time = row['就诊时间']
                if isinstance(visit_time, str):
                    try:
                        visit_time = datetime.strptime(visit_time, '%Y/%m/%d %H:%M')
                    except (ValueError, TypeError):
                        visit_time = datetime.now()
                elif not isinstance(visit_time, datetime):
                    visit_time = datetime.now()
                if hasattr(visit_time, 'to_pydatetime'):
                    visit_time = visit_time.to_pydatetime()
                visit_time = local_datetime_to_utc(visit_time)
                
                # 从姓名中提取姓氏，自动分配到对应的社区
                patient_name = str(row['姓名'])
                community = '其他村庄'
                if patient_name and len(patient_name) > 0 and patient_name != 'nan':
                    surname = patient_name[0]
                    if surname and surname.strip():
                        communities = surname_community_map.get(surname)
                        if communities:
                            # 如果一个姓氏对应多个村庄，随机选择一个
                            community = random.choice(communities)
                
                # 创建病历记录
                record = MedicalRecord(
                    patient_name=str(row['姓名']),
                    gender=str(row['性别']),
                    age=age,
                    visit_time=visit_time,
                    department=str(row['科室']) if pd.notna(row['科室']) else None,
                    doctor=str(row['医生']) if pd.notna(row['医生']) else None,
                    disease_category=str(row['疾病分类']) if pd.notna(row['疾病分类']) else None,
                    diagnosis=str(row['主诉']) if pd.notna(row['主诉']) else None,
                    medical_history=str(row['病历描述']) if pd.notna(row['病历描述']) else None,
                    insurance_type=str(row['医保']) if pd.notna(row['医保']) else None,
                    temperature=float(row['体温']) if pd.notna(row['体温']) else None,
                    heart_rate=float(row['心率']) if pd.notna(row['心率']) else None,
                    blood_pressure=str(row['血压']) if pd.notna(row['血压']) else None,
                    community=community
                )
                
                db.session.add(record)
                success_count += 1
                
                if success_count % 100 == 0:
                    print(f"已导入 {success_count} 条记录...")
                    db.session.commit()
                
            except Exception as e:
                print(f"导入第 {index + 1} 行时出错: {e}")
                continue
        
        db.session.commit()
        print(f"成功导入 {success_count} 条病历记录")

def import_communities():
    """从病历数据自动统计创建社区数据 - 根据实际数据中的社区自动创建"""
    with app.app_context():
        # 清空现有社区数据
        Community.query.delete()
        
        # 从病历数据中获取所有社区（自动识别）
        all_communities = db.session.query(MedicalRecord.community).distinct().all()
        community_names = [comm[0] for comm in all_communities if comm[0]]
        
        print(f"从病历数据中识别到 {len(community_names)} 个社区")
        
        # 获取实际村庄的详细信息
        community_details = get_community_detail_info()
        
        # 实际村庄的地理坐标（GCJ-02，用于高德地图）
        actual_geo = {
            '牛家垄周村': {'latitude': 29.331309, 'longitude': 116.204529},
            '岭背徐村': {'latitude': 29.334777, 'longitude': 116.20334},
            '徐家湾': {'latitude': 29.336276, 'longitude': 116.203794},
            '徐家咀': {'latitude': 29.341362, 'longitude': 116.201065},
            '竹峦徐村': {'latitude': 29.336287, 'longitude': 116.20108},
            '樟树湾徐村': {'latitude': 29.335991, 'longitude': 116.200526},
            '谭家新村': {'latitude': 29.331365, 'longitude': 116.201315},
            '新屋汪家': {'latitude': 29.341464, 'longitude': 116.197775},
            '新舍汪家': {'latitude': 29.338031, 'longitude': 116.19615},
            '段家颈村': {'latitude': 29.332383, 'longitude': 116.197219},
            '吴家仓': {'latitude': 29.333952, 'longitude': 116.205025},
            '庙北吴村': {'latitude': 29.271824, 'longitude': 116.203854},
            '茅棚邵村': {'latitude': 29.328062, 'longitude': 116.198161},
            '鲶鱼山邵村': {'latitude': 29.32061, 'longitude': 116.206059},
            '伍家湾村': {'latitude': 29.314924, 'longitude': 116.200542},
            '上下付村': {'latitude': 29.324107, 'longitude': 116.19828}
        }
        
        communities_data = []
        
        # 基础地理坐标（用于自动分配新村庄，可以后续手动调整）
        base_latitude = 28.5200
        base_longitude = 115.8300
        
        # 从病历数据统计每个社区的信息
        for idx, comm_name in enumerate(community_names):
            # 获取该社区的所有病历
            comm_records = MedicalRecord.query.filter_by(community=comm_name).all()
            
            if not comm_records:
                continue
            
            # 获取地理位置（优先使用实际坐标，否则自动分配）
            if comm_name in actual_geo:
                latitude = actual_geo[comm_name]['latitude']
                longitude = actual_geo[comm_name]['longitude']
            else:
                # 自动分配地理位置（按索引偏移）
                latitude = base_latitude + (idx % 5) * 0.01
                longitude = base_longitude + (idx // 5) * 0.01
            
            # 获取位置描述（如果有详细信息）
            location = comm_name
            if comm_name in community_details:
                location = community_details[comm_name]['location']
            
            # 统计总人数（去重姓名）
            unique_patients = set()
            total_age = 0
            elderly_count = 0
            chronic_disease_count = 0
            
            for record in comm_records:
                if record.patient_name:
                    unique_patients.add(record.patient_name)
                
                # 统计老年人
                if record.age and record.age >= 65:
                    elderly_count += 1
                    total_age += 1
                elif record.age:
                    total_age += 1
                
                # 统计慢性病（根据诊断判断）
                if record.diagnosis:
                    chronic_keywords = ['高血压', '糖尿病', '冠心病', '慢性', '关节炎']
                    if any(keyword in record.diagnosis for keyword in chronic_keywords):
                        chronic_disease_count += 1
            
            # 计算统计指标
            population = len(comm_records)  # 总病例数作为人口基数
            unique_patients_count = len(unique_patients)  # 唯一患者数
            elderly_ratio = elderly_count / total_age if total_age > 0 else 0
            chronic_ratio = chronic_disease_count / len(comm_records) if comm_records else 0
            
            communities_data.append({
                'name': comm_name,
                'location': location,  # 使用实际位置信息
                'latitude': latitude,
                'longitude': longitude,
                'population': population,
                'unique_patients': unique_patients_count,
                'elderly_ratio': round(elderly_ratio, 4),
                'chronic_disease_ratio': round(chronic_ratio, 4)
            })
        
        print(f"\n从病历数据统计得到的社区信息：")
        print("=" * 80)
        
        from services.health_risk_service import HealthRiskService
        health_service = HealthRiskService()
        
        for comm_data in communities_data:
            # 打印统计信息
            print(f"{comm_data['name']}:")
            print(f"  总病例数: {comm_data['population']}")
            print(f"  唯一患者数: {comm_data['unique_patients']}")
            print(f"  老年人比例: {comm_data['elderly_ratio']*100:.1f}%")
            print(f"  慢性病比例: {comm_data['chronic_disease_ratio']*100:.1f}%")
            
            # 计算脆弱性指数（基于真实统计数据）
            vulnerability_result = health_service.calculate_community_vulnerability_index({
                'elderly_ratio': comm_data['elderly_ratio'],
                'chronic_disease_ratio': comm_data['chronic_disease_ratio'],
                'medical_accessibility': 60,  # 假设值
                'env_quality_score': 70  # 假设值
            })
            
            print(f"  脆弱性指数: {vulnerability_result['vulnerability_index']}")
            print(f"  风险等级: {vulnerability_result['risk_level']}")
            print()
            
            community = Community(
                name=comm_data['name'],
                location=comm_data['location'],
                latitude=comm_data['latitude'],
                longitude=comm_data['longitude'],
                population=comm_data['population'],
                elderly_ratio=comm_data['elderly_ratio'],
                chronic_disease_ratio=comm_data['chronic_disease_ratio'],
                vulnerability_index=vulnerability_result['vulnerability_index'],
                risk_level=vulnerability_result['risk_level']
            )
            
            db.session.add(community)
        
        db.session.commit()
        print("=" * 80)
        print(f"成功创建 {len(communities_data)} 个社区（数据来自真实病历统计）")

def import_sample_weather_data():
    """导入示例天气数据"""
    with app.app_context():
        # 清空现有天气数据
        WeatherData.query.delete()
        
        communities = Community.query.all()
        today = today_local()
        
        from services.weather_service import WeatherService
        weather_service = WeatherService()
        
        # 为每个社区生成最近7天的天气数据
        from datetime import timedelta
        
        for i in range(7):
            date = today - timedelta(days=i)
            
            for community in communities:
                # 生成随机天气数据
                temp = random.uniform(10, 25)
                humidity = random.uniform(40, 80)
                aqi = random.randint(30, 150)
                
                weather_data = {
                    'temperature': temp,
                    'temperature_max': temp + random.uniform(2, 5),
                    'temperature_min': temp - random.uniform(2, 5),
                    'humidity': humidity,
                    'pressure': random.uniform(1000, 1020),
                    'weather_condition': random.choice(['晴', '多云', '阴', '小雨']),
                    'wind_speed': random.uniform(1, 8),
                    'pm25': aqi / 2,
                    'aqi': aqi
                }
                
                # 检测是否极端天气
                extreme_result = weather_service.identify_extreme_weather(weather_data)
                
                weather = WeatherData(
                    date=date,
                    location=community.name,
                    temperature=weather_data['temperature'],
                    temperature_max=weather_data['temperature_max'],
                    temperature_min=weather_data['temperature_min'],
                    humidity=weather_data['humidity'],
                    pressure=weather_data['pressure'],
                    weather_condition=weather_data['weather_condition'],
                    wind_speed=weather_data['wind_speed'],
                    pm25=weather_data['pm25'],
                    aqi=weather_data['aqi'],
                    is_extreme=extreme_result['is_extreme'],
                    extreme_type='、'.join([c['type'] for c in extreme_result['conditions']]) if extreme_result['is_extreme'] else None
                )
                
                db.session.add(weather)
        
        db.session.commit()
        print(f"成功导入天气数据")

if __name__ == '__main__':
    print("=" * 80)
    print("开始导入数据到数据库...")
    print("=" * 80)
    
    # 初始化数据库
    with app.app_context():
        db.create_all()
        print("数据库表创建完成")
    
    # 步骤1：扫描数据，自动识别姓氏
    print("\n1. 扫描数据，自动识别姓氏...")
    surname_community_map = scan_surnames_from_data()
    
    # 步骤2：导入病历数据（根据姓氏自动分配到社区）
    print("\n2. 导入病历数据...")
    import_medical_records(surname_community_map)
    
    # 步骤3：从病历数据自动统计创建社区数据
    print("\n3. 从病历数据自动创建社区...")
    import_communities()
    
    # 步骤4：导入天气数据
    print("\n4. 导入天气数据...")
    import_sample_weather_data()
    
    print("\n" + "=" * 80)
    print("数据导入完成！")
    print("=" * 80)
