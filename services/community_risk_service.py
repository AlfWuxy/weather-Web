# -*- coding: utf-8 -*-
"""
模块二：社区风险评估服务（改进版）

功能：
C1. 社区档案管理
C2. 脆弱性指数(VI)计算 - 使用可审计的线性指数
C3. 社区风险得分 & 地图生成
C4. Top N高风险社区清单
C5. 管控建议生成（医生端）

公式：
VI_c = 1 + a*老龄率 + b*慢病率 - d*绿地率 + ...

或使用回归模型：
log E[Y_{c,t}] = α + cb(Temp_t, lag) + s(time) + DOW + u_c + v_c · Heat_t
- u_c: 社区基线就诊水平差异
- v_c: 社区对高温（或寒冷）的额外敏感性（"天气脆弱性"）
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import json
import os
from flask import current_app, has_app_context


class CommunityRiskService:
    """社区风险评估服务"""
    
    def __init__(self):
        # 风险分数标度与阈值（基于历史分布校准）
        self.risk_score_scale = 7.0
        self.risk_level_thresholds = {
            'high': 80,
            'medium': 60
        }
        # VI权重参数（可审计、可调整）
        self.vi_weights = {
            'elderly_ratio': 1.5,      # 老龄率权重
            'chronic_disease_ratio': 1.8,  # 慢病率权重
            'green_space_ratio': -0.8,  # 绿地率权重（负向）
            'heat_island_index': 0.5,   # 热岛效应权重
            'medical_accessibility': -0.3  # 医疗可达性权重（负向）
        }
        
        # 社区敏感性参数（v_c）
        self.community_sensitivity = {}
        
        # 加载社区数据
        self._load_community_profiles()
    
    def _load_community_profiles(self):
        """加载社区档案数据"""
        # 从数据库或配置加载社区信息
        # 这里使用示例数据，实际应从Community表加载
        self.community_profiles = {}
        
        try:
            # 延迟导入避免循环导入问题
            # 只在Flask应用上下文中才能访问数据库
            from flask import current_app
            if current_app:
                from core.db_models import Community
                communities = Community.query.all()
                
                for comm in communities:
                    self.community_profiles[comm.name] = {
                        'id': comm.id,
                        'name': comm.name,
                        'location': comm.location,
                        'latitude': comm.latitude or 29.35,  # 默认都昌县坐标
                        'longitude': comm.longitude or 116.37,
                        'population': comm.population or 100,
                        'elderly_ratio': comm.elderly_ratio or 0.2,
                        'chronic_disease_ratio': comm.chronic_disease_ratio or 0.15,
                        'vulnerability_index': comm.vulnerability_index,
                        'risk_level': comm.risk_level,
                        
                        # 可扩展字段
                        'green_space_ratio': 0.1,  # 默认值，后续可更新
                        'heat_island_index': 0.5,  # 默认值
                        'medical_accessibility': 0.6,  # 默认值
                        'baseline_visits': 5  # 基线门诊水平
                    }
                
                if self.community_profiles:
                    print(f"✅ 加载 {len(self.community_profiles)} 个社区档案")
                    return
            
            # 如果没有应用上下文或没有数据，使用默认配置
            self._setup_default_communities()
            
        except RuntimeError:
            # 没有应用上下文时使用默认配置
            print("⚠️ 无Flask应用上下文，使用默认社区配置")
            self._setup_default_communities()
        except Exception as e:
            print(f"⚠️ 社区数据加载失败: {e}，使用默认配置")
            self._setup_default_communities()
    
    def _setup_default_communities(self):
        """设置默认社区配置"""
        coords_map = {}
        if has_app_context():
            try:
                coords_map = current_app.config.get('COMMUNITY_COORDS_GCJ') or {}
            except Exception:
                coords_map = {}

        default_communities = [
            {'name': '牛家垄周村', 'population': 132, 'elderly_ratio': 0.67, 'chronic_disease_ratio': 0.1},
            {'name': '岭背徐村', 'population': 89, 'elderly_ratio': 0.45, 'chronic_disease_ratio': 0.12},
            {'name': '徐家湾', 'population': 156, 'elderly_ratio': 0.38, 'chronic_disease_ratio': 0.15},
            {'name': '徐家咀', 'population': 98, 'elderly_ratio': 0.52, 'chronic_disease_ratio': 0.18},
            {'name': '竹峦徐村', 'population': 112, 'elderly_ratio': 0.41, 'chronic_disease_ratio': 0.11},
            {'name': '樟树湾徐村', 'population': 78, 'elderly_ratio': 0.55, 'chronic_disease_ratio': 0.14},
            {'name': '谭家新村', 'population': 145, 'elderly_ratio': 0.35, 'chronic_disease_ratio': 0.09},
            {'name': '新屋汪家', 'population': 92, 'elderly_ratio': 0.48, 'chronic_disease_ratio': 0.16},
        ]
        
        for i, comm in enumerate(default_communities):
            coords = coords_map.get(comm['name']) if coords_map else None
            if coords and len(coords) == 2:
                longitude, latitude = coords[0], coords[1]
            else:
                latitude = 29.35 + np.random.uniform(-0.05, 0.05)
                longitude = 116.37 + np.random.uniform(-0.05, 0.05)
            self.community_profiles[comm['name']] = {
                'id': i + 1,
                'name': comm['name'],
                'location': f"江西省九江市都昌县{comm['name']}",
                'latitude': latitude,
                'longitude': longitude,
                'population': comm['population'],
                'elderly_ratio': comm['elderly_ratio'],
                'chronic_disease_ratio': comm['chronic_disease_ratio'],
                'green_space_ratio': np.random.uniform(0.05, 0.25),
                'heat_island_index': np.random.uniform(0.3, 0.7),
                'medical_accessibility': np.random.uniform(0.4, 0.8),
                'baseline_visits': comm['population'] * 0.03  # 约3%日就诊率
            }
    
    def calculate_vulnerability_index(self, community_data):
        """
        计算社区脆弱性指数 (Vulnerability Index)
        
        公式: VI_c = 1 + a*老龄率 + b*慢病率 - d*绿地率 + e*热岛指数 - f*医疗可达性
        
        参数:
        - community_data: 社区数据字典
        
        返回:
        - vi: 脆弱性指数（>1表示比平均更脆弱）
        - breakdown: 各因子贡献分解
        """
        # 获取各因子值
        elderly_ratio = community_data.get('elderly_ratio', 0.2)
        chronic_ratio = community_data.get('chronic_disease_ratio', 0.15)
        green_ratio = community_data.get('green_space_ratio', 0.1)
        heat_island = community_data.get('heat_island_index', 0.5)
        medical_access = community_data.get('medical_accessibility', 0.5)
        
        # 计算各因子贡献
        breakdown = {
            'elderly_contribution': self.vi_weights['elderly_ratio'] * elderly_ratio,
            'chronic_contribution': self.vi_weights['chronic_disease_ratio'] * chronic_ratio,
            'green_contribution': self.vi_weights['green_space_ratio'] * green_ratio,
            'heat_island_contribution': self.vi_weights['heat_island_index'] * heat_island,
            'medical_contribution': self.vi_weights['medical_accessibility'] * medical_access
        }
        
        # 计算VI
        vi = 1.0
        for contribution in breakdown.values():
            vi += contribution
        
        # 确保VI >= 0.5
        vi = max(0.5, vi)
        
        # 确定脆弱性等级
        if vi >= 1.5:
            level = '高脆弱性'
            color = 'danger'
        elif vi >= 1.2:
            level = '中脆弱性'
            color = 'warning'
        else:
            level = '低脆弱性'
            color = 'success'
        
        return {
            'vulnerability_index': round(vi, 3),
            'level': level,
            'color': color,
            'breakdown': breakdown,
            'interpretation': f'该社区脆弱性指数为{vi:.2f}，{level}。'
                            f'主要因素：老龄率贡献{breakdown["elderly_contribution"]:.2f}，'
                            f'慢病率贡献{breakdown["chronic_contribution"]:.2f}'
        }
    
    def calculate_community_risk_score(self, community_name, weather_rr, target_date=None):
        """
        计算社区风险得分
        
        公式: RiskScore_c(t) = MacroRR(t) × VI_c × BaselineRate_c
        
        参数:
        - community_name: 社区名称
        - weather_rr: 宏观天气相对风险
        - target_date: 目标日期
        
        返回:
        - risk_score: 风险得分
        - details: 详细信息
        """
        if community_name not in self.community_profiles:
            return {'error': f'社区 {community_name} 未找到'}
        
        profile = self.community_profiles[community_name]
        
        # 计算VI
        vi_result = self.calculate_vulnerability_index(profile)
        vi = vi_result['vulnerability_index']
        
        # 获取基线门诊率
        baseline_rate = profile.get('baseline_visits', 5)
        
        # 计算风险得分
        risk_score = weather_rr * vi * baseline_rate
        
        # 标准化到0-100（固定尺度，保留跨天可比性）
        normalized_score = min(100, risk_score * self.risk_score_scale)
        
        # 确定风险等级
        if normalized_score >= self.risk_level_thresholds['high']:
            risk_level = '高风险'
            color = 'danger'
        elif normalized_score >= self.risk_level_thresholds['medium']:
            risk_level = '中风险'
            color = 'warning'
        else:
            risk_level = '低风险'
            color = 'success'
        
        return {
            'community': community_name,
            'risk_score': round(risk_score, 2),
            'normalized_score': round(normalized_score, 1),
            'risk_level': risk_level,
            'color': color,
            'components': {
                'weather_rr': round(weather_rr, 3),
                'vulnerability_index': vi,
                'baseline_rate': baseline_rate
            },
            'vi_details': vi_result,
            'population': profile.get('population', 0),
            'elderly_ratio': profile.get('elderly_ratio', 0),
            'expected_excess_visits': round((weather_rr - 1) * baseline_rate * vi, 1) if weather_rr > 1 else 0
        }
    
    def generate_community_risk_map(self, weather_data, target_date=None):
        """
        生成社区风险地图数据
        
        参数:
        - weather_data: 天气数据 {temperature, humidity, aqi, ...}
        - target_date: 目标日期
        
        返回:
        - map_data: 社区风险地图数据（GeoJSON格式）
        - rankings: 风险排名
        """
        from services.dlnm_risk_service import get_dlnm_service
        
        dlnm = get_dlnm_service()
        
        # 获取宏观天气RR
        temperature = weather_data.get('temperature', 20)
        macro_rr, _ = dlnm.calculate_rr(temperature)
        
        # 计算所有社区的风险
        community_risks = []
        
        for name, profile in self.community_profiles.items():
            risk = self.calculate_community_risk_score(name, macro_rr, target_date)
            risk['latitude'] = profile.get('latitude', 29.35)
            risk['longitude'] = profile.get('longitude', 116.37)
            community_risks.append(risk)
        
        # 按风险排序
        rankings = sorted(community_risks, key=lambda x: x['normalized_score'], reverse=True)

        # 计算当日相对指数与分位
        risk_scores = [r.get('risk_score', 0) for r in community_risks if isinstance(r, dict)]
        mean_score = sum(risk_scores) / len(risk_scores) if risk_scores else 0
        score_array = np.array(risk_scores, dtype=float) if risk_scores else np.array([])

        for risk in community_risks:
            score = float(risk.get('risk_score', 0))
            if mean_score > 0:
                risk['relative_index'] = round(score / mean_score * 100, 1)
            else:
                risk['relative_index'] = 100.0

            if score_array.size:
                less_count = np.sum(score_array < score)
                equal_count = np.sum(score_array == score)
                percentile = (less_count + 0.5 * equal_count) / score_array.size * 100
                risk['percentile_rank'] = round(float(percentile), 1)
            else:
                risk['percentile_rank'] = 0.0
        
        # 生成GeoJSON格式数据
        geojson_features = []
        for risk in community_risks:
            feature = {
                'type': 'Feature',
                'geometry': {
                    'type': 'Point',
                    'coordinates': [risk['longitude'], risk['latitude']]
                },
                'properties': {
                    'name': risk['community'],
                    'risk_score': risk['normalized_score'],
                    'risk_level': risk['risk_level'],
                    'color': risk['color'],
                    'population': risk['population'],
                    'elderly_ratio': risk['elderly_ratio'],
                    'vi': risk['components']['vulnerability_index'],
                    'relative_index': risk.get('relative_index', 100.0),
                    'percentile_rank': risk.get('percentile_rank', 0.0)
                }
            }
            geojson_features.append(feature)
        
        map_data = {
            'type': 'FeatureCollection',
            'features': geojson_features
        }
        
        # 生成管控建议
        management_suggestions = self._generate_management_suggestions(rankings[:5], weather_data)
        
        return {
            'map_data': map_data,
            'rankings': [
                {
                    'rank': i + 1,
                    'community': r['community'],
                    'risk_score': r['normalized_score'],
                    'risk_level': r['risk_level'],
                    'population': r['population'],
                    'expected_excess_visits': r['expected_excess_visits'],
                    'relative_index': r.get('relative_index', 100.0),
                    'percentile_rank': r.get('percentile_rank', 0.0)
                }
                for i, r in enumerate(rankings)
            ],
            'summary': {
                'total_communities': len(rankings),
                'high_risk_count': sum(1 for r in rankings if r['risk_level'] == '高风险'),
                'medium_risk_count': sum(1 for r in rankings if r['risk_level'] == '中风险'),
                'low_risk_count': sum(1 for r in rankings if r['risk_level'] == '低风险'),
                'total_expected_excess': sum(r['expected_excess_visits'] for r in rankings)
            },
            'macro_weather': {
                'temperature': temperature,
                'rr': round(macro_rr, 3)
            },
            'management_suggestions': management_suggestions
        }
    
    def _generate_management_suggestions(self, high_risk_communities, weather_data):
        """生成管控建议（医生端）"""
        suggestions = []
        
        temp = weather_data.get('temperature', 20)
        
        # 资源调度建议
        if len(high_risk_communities) >= 3:
            suggestions.append({
                'category': '资源调配',
                'priority': 'high',
                'advice': f'建议向 {high_risk_communities[0]["community"]}、{high_risk_communities[1]["community"]} 等高风险社区增派医疗资源',
                'target_communities': [c['community'] for c in high_risk_communities[:3]]
            })
        
        # 巡访建议
        for comm in high_risk_communities[:3]:
            if comm.get('elderly_ratio', 0) > 0.4:
                suggestions.append({
                    'category': '健康巡访',
                    'priority': 'high',
                    'advice': f'{comm["community"]} 老龄化程度高({comm["elderly_ratio"]*100:.0f}%)，建议加强独居老人巡访',
                    'target_communities': [comm['community']]
                })
        
        # 温度相关建议
        if temp > 32:
            suggestions.append({
                'category': '防暑措施',
                'priority': 'high',
                'advice': '高温天气，建议在高风险社区开放避暑点、发放防暑物资',
                'target_communities': [c['community'] for c in high_risk_communities]
            })
        elif temp < 5:
            suggestions.append({
                'category': '防寒措施',
                'priority': 'high',
                'advice': '低温天气，建议检查高风险社区供暖情况、关注独居老人',
                'target_communities': [c['community'] for c in high_risk_communities]
            })
        
        # 门诊准备
        total_excess = sum(c.get('expected_excess_visits', 0) for c in high_risk_communities)
        if total_excess > 10:
            suggestions.append({
                'category': '门诊准备',
                'priority': 'medium',
                'advice': f'预计高风险社区额外增加约 {total_excess:.0f} 人次就诊，建议门诊做好准备',
                'target_communities': [c['community'] for c in high_risk_communities]
            })
        
        if not suggestions:
            suggestions.append({
                'category': '常规管理',
                'priority': 'low',
                'advice': '各社区风险处于正常水平，保持常规健康管理工作',
                'target_communities': []
            })
        
        return suggestions
    
    def update_community_sensitivity(self, community_name, heat_sensitivity=None, cold_sensitivity=None):
        """
        更新社区天气敏感性参数 (v_c)
        
        这是模型中的关键参数，表示社区对高温/寒冷的额外敏感性
        """
        if community_name not in self.community_sensitivity:
            self.community_sensitivity[community_name] = {
                'heat_sensitivity': 1.0,
                'cold_sensitivity': 1.0
            }
        
        if heat_sensitivity is not None:
            self.community_sensitivity[community_name]['heat_sensitivity'] = heat_sensitivity
        
        if cold_sensitivity is not None:
            self.community_sensitivity[community_name]['cold_sensitivity'] = cold_sensitivity
    
    def get_community_profile(self, community_name):
        """获取社区档案"""
        if community_name in self.community_profiles:
            profile = self.community_profiles[community_name].copy()
            vi_result = self.calculate_vulnerability_index(profile)
            profile['vulnerability_details'] = vi_result
            return profile
        return None
    
    def get_all_communities(self):
        """获取所有社区列表"""
        communities = []
        for name, profile in self.community_profiles.items():
            vi_result = self.calculate_vulnerability_index(profile)
            communities.append({
                'name': name,
                'population': profile.get('population', 0),
                'elderly_ratio': profile.get('elderly_ratio', 0),
                'chronic_disease_ratio': profile.get('chronic_disease_ratio', 0),
                'vulnerability_index': vi_result['vulnerability_index'],
                'vulnerability_level': vi_result['level']
            })
        
        # 按VI排序
        return sorted(communities, key=lambda x: x['vulnerability_index'], reverse=True)


# 单例实例
_community_service = None

def get_community_service():
    """获取社区风险服务单例"""
    global _community_service
    if _community_service is None:
        _community_service = CommunityRiskService()
    return _community_service


# 测试代码
if __name__ == '__main__':
    print("=" * 60)
    print("社区风险评估服务测试")
    print("=" * 60)
    
    service = CommunityRiskService()
    
    print("\n所有社区列表:")
    communities = service.get_all_communities()
    for comm in communities:
        print(f"  {comm['name']}: VI={comm['vulnerability_index']:.2f}, "
              f"老龄率={comm['elderly_ratio']*100:.0f}%, "
              f"级别={comm['vulnerability_level']}")
    
    print("\n社区风险地图生成测试:")
    weather = {'temperature': 35, 'humidity': 80, 'aqi': 120}
    result = service.generate_community_risk_map(weather)
    
    print(f"\n风险摘要:")
    print(f"  高风险社区: {result['summary']['high_risk_count']} 个")
    print(f"  中风险社区: {result['summary']['medium_risk_count']} 个")
    print(f"  低风险社区: {result['summary']['low_risk_count']} 个")
    
    print("\n风险排名Top 3:")
    for r in result['rankings'][:3]:
        print(f"  {r['rank']}. {r['community']}: "
              f"风险分数={r['risk_score']}, {r['risk_level']}")
    
    print("\n管控建议:")
    for s in result['management_suggestions']:
        print(f"  [{s['priority']}] {s['category']}: {s['advice']}")

