# -*- coding: utf-8 -*-
"""
和风天气API测试脚本
用于诊断API调用问题
"""
import os
import requests
import pytest

pytestmark = pytest.mark.manual

def test_qweather_api():
    """测试和风天气API"""
    print("=" * 50)
    print("和风天气API测试")
    print("=" * 50)
    
    # API配置（如使用付费订阅版，请在本地环境变量里显式提供专属域名）
    key = os.getenv("QWEATHER_KEY")
    base_url = os.getenv("QWEATHER_API_BASE")
    location = "116.20,29.27"  # 都昌县

    if not key:
        pytest.skip("未设置 QWEATHER_KEY")
    if not base_url:
        pytest.skip("未设置 QWEATHER_API_BASE")
    if "your-qweather-host.example.com" in base_url:
        pytest.skip("QWEATHER_API_BASE 仍是占位值")
    
    print("\nAPI Key: 已配置")
    print(f"Base URL: {base_url}")
    print(f"Location: {location}")
    
    # 测试1: 实时天气
    print("\n" + "-" * 40)
    print("测试1: 实时天气 (/weather/now)")
    print("-" * 40)
    
    try:
        url = f"{base_url}/weather/now"
        params = {'key': key, 'location': location}
        
        print(f"请求URL: {url}")
        response = requests.get(url, params=params, timeout=10)
        
        print(f"HTTP状态码: {response.status_code}")
        
        assert response.status_code == 200, f"实况天气 HTTP {response.status_code}"
        data = response.json()
        code = data.get('code')
        assert code == '200', f"实况天气业务码 {code}"
        now = data.get('now') or {}
        assert now.get('temp') is not None
        assert now.get('humidity') is not None
            
    except requests.RequestException as exc:
        pytest.fail(f"实况天气请求失败: {type(exc).__name__}")
    
    # 测试2: 7天预报
    print("\n" + "-" * 40)
    print("测试2: 7天预报 (/weather/7d)")
    print("-" * 40)
    
    try:
        url = f"{base_url}/weather/7d"
        params = {'key': key, 'location': location}
        
        response = requests.get(url, params=params, timeout=10)
        print(f"HTTP状态码: {response.status_code}")
        
        assert response.status_code == 200, f"7天预报 HTTP {response.status_code}"
        data = response.json()
        code = data.get('code')
        assert code == '200', f"7天预报业务码 {code}"
        daily = data.get('daily') or []
        assert len(daily) >= 7
            
    except requests.RequestException as exc:
        pytest.fail(f"7天预报请求失败: {type(exc).__name__}")
    
    # 测试3: 空气质量
    print("\n" + "-" * 40)
    print("测试3: 空气质量 (/air/now)")
    print("-" * 40)
    
    try:
        url = f"{base_url}/air/now"
        params = {'key': key, 'location': location}
        
        response = requests.get(url, params=params, timeout=10)
        print(f"HTTP状态码: {response.status_code}")
        
        assert response.status_code == 200, f"空气质量 HTTP {response.status_code}"
        data = response.json()
        code = data.get('code')
        assert code in {'200', '204'}, f"空气质量业务码 {code}"
        if code == '200':
            now = data.get('now') or {}
            assert now.get('aqi') is not None
            assert now.get('pm2p5') is not None
            
    except requests.RequestException as exc:
        pytest.fail(f"空气质量请求失败: {type(exc).__name__}")
    
    print("\n" + "=" * 50)
    print("测试完成")
    print("=" * 50)

if __name__ == '__main__':
    test_qweather_api()

