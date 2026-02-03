# -*- coding: utf-8 -*-
"""
测试脚本 - 验证所有新服务模块
"""
import json
import sys
from pathlib import Path

import pytest

# 添加项目根目录到路径
ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR))

pytestmark = pytest.mark.manual


def test_dlnm_service():
    """测试DLNM风险函数服务"""
    print("\n" + "=" * 60)
    print("1. 测试DLNM风险函数服务")
    print("=" * 60)
    
    try:
        from services.dlnm_risk_service import DLNMRiskService
        
        service = DLNMRiskService()
        
        # 测试模型摘要
        summary = service.get_model_summary()
        print(f"\n模型状态: {summary.get('status', '未知')}")
        if summary.get('mmt'):
            print(f"最低风险温度(MMT): {summary['mmt']:.1f}°C")
        
        # 测试不同温度的RR
        print("\n温度-RR映射测试:")
        for temp in [-5, 5, 15, 25, 35]:
            rr, breakdown = service.calculate_rr(temp)
            print(f"  温度 {temp:3d}°C: RR = {rr:.3f}")
        
        # 测试极端天气识别
        print("\n极端天气识别测试:")
        events = service.identify_extreme_weather_events(38, duration=3)
        for event in events:
            print(f"  {event['type']}: {event['description']}")
        
        print("\n✅ DLNM服务测试通过")
        return True
        
    except Exception as e:
        print(f"\n❌ DLNM服务测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_forecast_service():
    """测试天气预报与健康预测服务"""
    print("\n" + "=" * 60)
    print("2. 测试天气预报与健康预测服务")
    print("=" * 60)
    
    try:
        from services.forecast_service import ForecastService
        
        service = ForecastService()
        
        # 测试服务状态
        status = service.get_service_status()
        print(f"\n历史天气数据: {status['weather_history_days']} 天")
        print(f"门诊量P90阈值: {status['visit_threshold_p90']}")
        
        # 测试7天预测
        print("\n7天健康预测测试:")
        forecast_temps = [15, 18, 22, 28, 32, 25, 18]  # 模拟预报温度
        forecasts, summary = service.generate_7day_forecast(forecast_temps)
        
        print(f"预测期间: {summary['forecast_period']['start']} 至 {summary['forecast_period']['end']}")
        print(f"预计总门诊量: {summary['total_expected_visits']:.0f} 人次")
        print(f"高风险天数: {summary['high_risk_days']} 天")
        
        print("\n每日预测:")
        for f in forecasts[:3]:  # 只显示前3天
            print(f"  {f['date']} ({f['day_of_week']}): "
                  f"温度{f['temperature']['corrected']:.1f}°C, "
                  f"预计{f['visits']['point_estimate']}人次, "
                  f"{f['risk_level']}")
        
        print("\n✅ 预报服务测试通过")
        return True
        
    except Exception as e:
        print(f"\n❌ 预报服务测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_community_service():
    """测试社区风险评估服务"""
    print("\n" + "=" * 60)
    print("3. 测试社区风险评估服务")
    print("=" * 60)
    
    try:
        from services.community_risk_service import CommunityRiskService
        
        service = CommunityRiskService()
        
        # 测试社区列表
        communities = service.get_all_communities()
        print(f"\n已加载社区数: {len(communities)} 个")
        
        if communities:
            print("\n社区脆弱性排名 (Top 3):")
            for i, comm in enumerate(communities[:3]):
                print(f"  {i+1}. {comm['name']}: VI={comm['vulnerability_index']:.2f}, "
                      f"老龄率={comm['elderly_ratio']*100:.0f}%")
        
        # 测试风险地图
        print("\n社区风险地图测试 (35°C高温):")
        weather = {'temperature': 35, 'humidity': 80, 'aqi': 100}
        result = service.generate_community_risk_map(weather)
        
        print(f"高风险社区: {result['summary']['high_risk_count']} 个")
        print(f"中风险社区: {result['summary']['medium_risk_count']} 个")
        
        if result['rankings']:
            print(f"\n最高风险社区: {result['rankings'][0]['community']} "
                  f"(风险分数: {result['rankings'][0]['risk_score']})")
        
        print("\n✅ 社区服务测试通过")
        return True
        
    except Exception as e:
        print(f"\n❌ 社区服务测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_chronic_service():
    """测试慢病风险预测服务"""
    print("\n" + "=" * 60)
    print("4. 测试慢病风险预测服务")
    print("=" * 60)
    
    try:
        from services.chronic_risk_service import ChronicRiskService
        
        service = ChronicRiskService()
        
        # 测试规则库
        rules = service.get_rules_version()
        print(f"\n规则库版本: {rules['version']}")
        print(f"规则总数: {rules['total_rules']}")
        
        # 测试个体风险预测
        print("\n个体风险预测测试:")
        
        # 用例1：老年高血压患者 + 高温
        user1 = {'age': 72, 'chronic_diseases': ['高血压', '冠心病']}
        weather1 = {'temperature': 35, 'humidity': 85, 'aqi': 80}
        
        result1 = service.predict_individual_risk(user1, weather1)
        print(f"\n用例1: 72岁高血压冠心病患者 + 35°C高温")
        print(f"  总体风险: {result1['overall_risk']['level']} (RR={result1['overall_risk']['rr']:.2f})")
        if result1['recommendations']:
            print(f"  首要建议: {result1['recommendations'][0]['advice'][:50]}...")
        
        # 用例2：老年COPD患者 + 低温
        user2 = {'age': 68, 'chronic_diseases': ['COPD']}
        weather2 = {'temperature': 2, 'humidity': 60, 'aqi': 120}
        
        result2 = service.predict_individual_risk(user2, weather2)
        print(f"\n用例2: 68岁COPD患者 + 2°C低温 + AQI 120")
        print(f"  总体风险: {result2['overall_risk']['level']} (RR={result2['overall_risk']['rr']:.2f})")
        if result2['recommendations']:
            print(f"  首要建议: {result2['recommendations'][0]['advice'][:50]}...")
        
        print("\n✅ 慢病服务测试通过")
        return True
        
    except Exception as e:
        print(f"\n❌ 慢病服务测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_integration():
    """测试服务集成"""
    print("\n" + "=" * 60)
    print("5. 测试服务集成 (综合预警)")
    print("=" * 60)
    
    try:
        from services.dlnm_risk_service import get_dlnm_service
        from services.forecast_service import get_forecast_service
        from services.community_risk_service import get_community_service
        from services.chronic_risk_service import get_chronic_service
        
        # 获取所有服务
        dlnm = get_dlnm_service()
        forecast = get_forecast_service()
        community = get_community_service()
        chronic = get_chronic_service()
        
        # 模拟场景：极端高温
        temperature = 38
        weather = {'temperature': temperature, 'humidity': 90, 'aqi': 100}
        
        print(f"\n场景: 极端高温 {temperature}°C")
        
        # 1. DLNM风险
        rr, _ = dlnm.calculate_rr(temperature)
        events = dlnm.identify_extreme_weather_events(temperature)
        print(f"\n宏观RR: {rr:.3f}")
        print(f"极端事件: {[e['type'] for e in events]}")
        
        # 2. 社区风险
        comm_result = community.generate_community_risk_map(weather)
        print(f"\n社区风险摘要:")
        print(f"  高风险社区: {comm_result['summary']['high_risk_count']} 个")
        
        # 3. 7天预测
        forecast_temps = [38, 36, 35, 32, 28, 25, 22]  # 高温后降温
        forecasts, summary = forecast.generate_7day_forecast(forecast_temps)
        print(f"\n7天预测摘要:")
        print(f"  高风险天数: {summary['high_risk_days']} 天")
        print(f"  预计总门诊: {summary['total_expected_visits']:.0f} 人次")
        
        # 4. 慢病风险
        user = {'age': 75, 'chronic_diseases': ['高血压', '冠心病']}
        chronic_result = chronic.predict_individual_risk(user, weather)
        print(f"\n75岁心血管患者风险:")
        print(f"  风险等级: {chronic_result['overall_risk']['level']}")
        
        # 确定综合预警
        if rr >= 1.4 or summary['high_risk_days'] >= 3:
            alert = '红色预警'
        elif rr >= 1.2 or summary['high_risk_days'] >= 1:
            alert = '橙色预警'
        else:
            alert = '正常'
        
        print(f"\n综合预警级别: {alert}")
        
        print("\n✅ 集成测试通过")
        return True
        
    except Exception as e:
        print(f"\n❌ 集成测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """运行所有测试"""
    print("\n" + "=" * 70)
    print("天气-健康风险预测系统 - 服务测试")
    print("=" * 70)
    
    results = {
        'DLNM风险函数': test_dlnm_service(),
        '天气预报与健康预测': test_forecast_service(),
        '社区风险评估': test_community_service(),
        '慢病风险预测': test_chronic_service(),
        '服务集成': test_integration()
    }
    
    # 汇总结果
    print("\n" + "=" * 70)
    print("测试结果汇总")
    print("=" * 70)
    
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    
    for name, result in results.items():
        status = "✅ 通过" if result else "❌ 失败"
        print(f"  {name}: {status}")
    
    print(f"\n总计: {passed}/{total} 通过")
    
    if passed == total:
        print("\n🎉 所有测试通过！系统就绪。")
    else:
        print("\n⚠️ 部分测试失败，请检查错误信息。")
    
    return passed == total


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)

