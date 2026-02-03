# -*- coding: utf-8 -*-
"""
和风天气API测试脚本
用于诊断API调用问题
"""
import os
import requests

def test_qweather_api():
    """测试和风天气API"""
    print("=" * 50)
    print("和风天气API测试")
    print("=" * 50)
    
    # API配置（付费订阅版使用专属域名）
    key = os.getenv("QWEATHER_KEY")
    # 付费版API使用专属Host，不是devapi.qweather.com
    base_url = os.getenv("QWEATHER_API_BASE", "https://mj76x98pfn.re.qweatherapi.com/v7")
    location = "116.20,29.27"  # 都昌县

    if not key:
        print("❌ 未设置环境变量 QWEATHER_KEY，无法测试。")
        return
    
    print(f"\nAPI Key: {key[:6]}...{key[-4:]}")
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
        
        if response.status_code == 200:
            data = response.json()
            code = data.get('code')
            print(f"API返回码: {code}")
            
            if code == '200':
                now = data.get('now', {})
                print(f"✅ 成功！")
                print(f"   温度: {now.get('temp')}°C")
                print(f"   天气: {now.get('text')}")
                print(f"   湿度: {now.get('humidity')}%")
                print(f"   风速: {now.get('windSpeed')} km/h")
            else:
                print(f"❌ API返回错误码: {code}")
                print(f"   完整响应: {data}")
        else:
            print(f"❌ HTTP错误: {response.status_code}")
            print(f"   响应内容: {response.text[:500]}")
            
    except requests.exceptions.Timeout:
        print("❌ 请求超时")
    except requests.exceptions.ConnectionError as e:
        print(f"❌ 连接错误: {e}")
    except Exception as e:
        print(f"❌ 未知错误: {e}")
    
    # 测试2: 7天预报
    print("\n" + "-" * 40)
    print("测试2: 7天预报 (/weather/7d)")
    print("-" * 40)
    
    try:
        url = f"{base_url}/weather/7d"
        params = {'key': key, 'location': location}
        
        response = requests.get(url, params=params, timeout=10)
        print(f"HTTP状态码: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            code = data.get('code')
            print(f"API返回码: {code}")
            
            if code == '200':
                daily = data.get('daily', [])
                print(f"✅ 成功！获取到 {len(daily)} 天预报")
                for day in daily[:3]:
                    print(f"   {day.get('fxDate')}: {day.get('tempMin')}~{day.get('tempMax')}°C, {day.get('textDay')}")
            else:
                print(f"❌ API返回错误码: {code}")
        else:
            print(f"❌ HTTP错误")
            
    except Exception as e:
        print(f"❌ 错误: {e}")
    
    # 测试3: 空气质量
    print("\n" + "-" * 40)
    print("测试3: 空气质量 (/air/now)")
    print("-" * 40)
    
    try:
        url = f"{base_url}/air/now"
        params = {'key': key, 'location': location}
        
        response = requests.get(url, params=params, timeout=10)
        print(f"HTTP状态码: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            code = data.get('code')
            print(f"API返回码: {code}")
            
            if code == '200':
                now = data.get('now', {})
                print(f"✅ 成功！")
                print(f"   AQI: {now.get('aqi')}")
                print(f"   PM2.5: {now.get('pm2p5')}")
                print(f"   空气质量: {now.get('category')}")
            else:
                print(f"⚠️ API返回码: {code} (空气质量数据可能不可用)")
        else:
            print(f"❌ HTTP错误")
            
    except Exception as e:
        print(f"❌ 错误: {e}")
    
    print("\n" + "=" * 50)
    print("测试完成")
    print("=" * 50)

if __name__ == '__main__':
    test_qweather_api()

