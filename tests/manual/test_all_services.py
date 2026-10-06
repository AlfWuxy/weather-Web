# -*- coding: utf-8 -*-
"""
全面测试所有服务模块
"""
import sys
import traceback
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR))

pytestmark = pytest.mark.manual

def test_dlnm_service():
    """测试 DLNM 风险服务"""
    print('\n1. 测试 DLNM 风险服务...')
    try:
        from services.dlnm_risk_service import DLNMRiskService
        dlnm = DLNMRiskService()
        
        # 测试RR计算
        rr, breakdown = dlnm.calculate_rr(25)
        print(f'   ✅ RR计算正常: 温度25°C的RR={rr:.3f}')
        
        # 测试极端天气识别
        events = dlnm.identify_extreme_weather_events(38)
        print(f'   ✅ 极端天气识别: {len(events)}个事件')
        
        # 测试模型摘要
        summary = dlnm.get_model_summary()
        print(f'   ✅ 模型状态: {summary.get("status", "未知")}')
        if dlnm.mmt:
            print(f'   MMT: {dlnm.mmt:.1f}°C')
        
        return
    except Exception as e:
        print(f'   ❌ 错误: {e}')
        traceback.print_exc()
        pytest.fail(f"DLNM风险服务测试失败: {e}")

def test_forecast_service():
    """测试预报服务"""
    print('\n2. 测试预报服务...')
    try:
        from services.forecast_service import ForecastService
        fs = ForecastService()
        
        status = fs.get_service_status()
        print(f'   ✅ 服务状态正常: 历史数据{status["weather_history_days"]}天')
        
        # 测试7天预测
        forecast_temps = [20, 22, 25, 23, 21, 19, 18]
        # 显式演练缺少真实门诊阈值，避免依赖本机私有研究文件。
        with patch.object(fs, 'visit_threshold_p90', None):
            forecasts, summary = fs.generate_7day_forecast(forecast_temps)
        assert len(forecasts) == 7
        assert summary['total_expected_visits'] is None
        assert summary['average_daily_visits'] is None
        assert summary['unknown_health_days'] == 7
        assert summary['visit_projection_status'] == 'unknown'
        assert summary['high_risk_days'] is None
        assert summary['model_warning_status'] == 'disabled_uncalibrated'
        assert summary['probability_products']['available'] is False
        for row in forecasts:
            assert row['visits']['status'] == 'unknown'
            assert 'historical_visit_threshold' in row['visits']['missing_inputs']
            assert row['visits']['point_estimate'] is None
            assert row['probability_high_visits'] is None
            assert row['risk_level'] is None
        print(f'   ✅ 7天预测成功: 高风险天数={summary["high_risk_days"]}')
        print('   预计总门诊: 未知（缺少真实门诊阈值，不生成伪零）')
        
        return
    except Exception as e:
        print(f'   ❌ 错误: {e}')
        traceback.print_exc()
        pytest.fail(f"预报服务测试失败: {e}")

def test_community_service():
    """测试社区风险服务"""
    print('\n3. 测试社区风险服务...')
    try:
        from services.community_risk_service import CommunityRiskService
        cs = CommunityRiskService()
        
        communities = cs.get_all_communities()
        print(f'   ✅ 加载社区: {len(communities)}个')
        
        # 测试脆弱性计算
        vi = cs.calculate_vulnerability_index({
            'elderly_ratio': 0.5,
            'chronic_disease_ratio': 0.2,
            'green_space_ratio': 0.1
        })
        assert vi['vulnerability_index'] is None
        assert vi['ranking_eligible'] is False
        assert {'heat_island_index', 'medical_accessibility'} <= set(vi['missing_fields'])
        print(f'   ✅ 脆弱性门禁: {vi["level"]}（不生成伪分数）')
        
        # 测试风险地图生成
        result = cs.generate_community_risk_map({'temperature': 30})
        ranked = int((result.get('summary') or {}).get('ranked_communities') or 0)
        print(f'   ✅ 风险地图门禁: {ranked}个社区具备排名证据')
        
        return
    except Exception as e:
        print(f'   ❌ 错误: {e}')
        traceback.print_exc()
        pytest.fail(f"社区风险服务测试失败: {e}")

def test_chronic_service():
    """测试慢病风险服务"""
    print('\n4. 测试慢病风险服务...')
    try:
        from services.chronic_risk_service import ChronicRiskService
        cr = ChronicRiskService()
        
        # 测试个体风险预测
        result = cr.predict_individual_risk(
            {'age': 70, 'chronic_diseases': ['高血压', '冠心病']},
            {'temperature': 35, 'aqi': 100}
        )
        print(f'   ✅ 个体风险预测: 等级={result["overall_risk"]["level"]}')
        assert result['overall_risk']['rr'] is None
        assert result['overall_risk']['score'] is None
        assert result['overall_risk']['level'] == '风险未知'
        assert result['data_quality']['missing_fields'] == ['humidity']
        assert result['input_states']['humidity']['status'] == 'unknown'
        assert result['requires_followup'] is True
        assert result['followup_priority'] == 'medium'
        print('   RR=未知（湿度缺失，进入回访）')
        print(f'   建议数: {len(result["recommendations"])}')
        
        # 测试人群风险预测
        pop_result = cr.predict_population_risk({}, {'temperature': 35})
        assert pop_result['status'] == 'unknown'
        assert set(pop_result['data_quality']['missing_fields']) == {'aqi', 'humidity'}
        assert pop_result['overall_summary']['highest_risk_group'] is None
        assert pop_result['overall_summary']['high_risk_count'] is None
        assert pop_result['overall_summary']['highest_rr'] is None
        assert pop_result['requires_followup'] is True
        print(f'   ✅ 人群风险预测: 最高风险群体={pop_result["overall_summary"]["highest_risk_group"]}')
        
        return
    except Exception as e:
        print(f'   ❌ 错误: {e}')
        traceback.print_exc()
        pytest.fail(f"慢病风险服务测试失败: {e}")

@pytest.mark.network
def test_weather_service():
    """测试天气服务"""
    print('\n5. 测试天气服务...')
    try:
        from services.weather_service import WeatherService
        ws = WeatherService()
        
        # 测试获取天气
        weather = ws.get_current_weather('北京')
        print(f'   ✅ 获取天气成功: 温度={weather["temperature"]}°C')
        
        # 测试极端天气识别
        extreme = ws.identify_extreme_weather(weather)
        print(f'   ✅ 极端天气识别: 是否极端={extreme["is_extreme"]}')
        
        return
    except Exception as e:
        print(f'   ❌ 错误: {e}')
        traceback.print_exc()
        pytest.fail(f"天气服务测试失败: {e}")


def _run_manual_case(test_func):
    """Allow running pytest-style tests from __main__ for manual smoke checks."""
    try:
        test_func()
        return True
    except BaseException:
        return False

if __name__ == '__main__':
    print('=' * 60)
    print('全面测试所有服务模块')
    print('=' * 60)
    
    results = {
        'DLNM风险服务': _run_manual_case(test_dlnm_service),
        '预报服务': _run_manual_case(test_forecast_service),
        '社区风险服务': _run_manual_case(test_community_service),
        '慢病风险服务': _run_manual_case(test_chronic_service),
        '天气服务': _run_manual_case(test_weather_service)
    }
    
    print('\n' + '=' * 60)
    print('测试结果汇总')
    print('=' * 60)
    
    passed = 0
    failed = 0
    for name, result in results.items():
        status = '✅ 通过' if result else '❌ 失败'
        print(f'  {name}: {status}')
        if result:
            passed += 1
        else:
            failed += 1
    
    print(f'\n总计: {passed} 通过, {failed} 失败')
    
    if failed > 0:
        sys.exit(1)

