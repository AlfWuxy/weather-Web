# -*- coding: utf-8 -*-
"""分析温度-门诊量分布"""
import pandas as pd
import numpy as np
from pathlib import Path

# 加载数据
base_dir = Path(__file__).resolve().parents[1]
weather_path = base_dir / 'data' / 'raw' / '逐日数据.csv'
medical_path = base_dir / 'data' / 'research' / '数据.xlsx'

# 加载天气数据
weather_df = pd.read_csv(weather_path, encoding='utf-8')
weather_df.columns = list(c.strip() for c in weather_df.columns)

# 找到温度列
for col in weather_df.columns:
    if '平均气温' in col:
        weather_df['tmean'] = pd.to_numeric(weather_df[col], errors='coerce')
        break

# 找到日期列
for col in weather_df.columns:
    if '日期' in col:
        weather_df['date'] = pd.to_datetime(weather_df[col])
        break

weather_df['date_only'] = weather_df['date'].dt.date

# 加载病历数据
medical_df = pd.read_excel(medical_path, header=None)
medical_df.columns = ['序号', '医保', '姓名', '性别', '年龄', '就诊时间',
                      '科室', '医生', '疾病分类', '主诉', '病历描述',
                      '列11', '体温', '心率', '血压']
medical_df['就诊时间'] = pd.to_datetime(medical_df['就诊时间'])
medical_df['date_only'] = medical_df['就诊时间'].dt.date

# 按日期统计门诊量
daily_visits = medical_df.groupby('date_only').size().reset_index(name='visits')

# 合并
merged = pd.merge(daily_visits, weather_df[['date_only', 'tmean']], on='date_only', how='inner')
merged = merged.dropna(subset=['tmean', 'visits'])

print('=' * 60)
print('温度-门诊量分布分析')
print('=' * 60)

# 按温度区间分析
bins = [0, 5, 8, 10, 12, 15, 18, 20, 22, 25, 28, 30, 35]
labels = ['0-5', '5-8', '8-10', '10-12', '12-15', '15-18', '18-20', '20-22', '22-25', '25-28', '28-30', '30-35']
merged['temp_bin'] = pd.cut(merged['tmean'], bins=bins, labels=labels, include_lowest=True)

print('\n各温度区间门诊量统计:')
print('-' * 60)
stats = merged.groupby('temp_bin', observed=False).agg({
    'visits': ['count', 'mean', 'std', 'min', 'max', 'sum']
}).round(2)
stats.columns = ['天数', '日均门诊', '标准差', '最小', '最大', '总门诊']
print(stats.to_string())

# 找MMT区间（15-20度）的基线
mmt_mask = (merged['tmean'] >= 15) & (merged['tmean'] <= 20)
baseline = merged[mmt_mask]['visits'].mean()
print(f'\n基线门诊量 (15-20°C): {baseline:.2f} 人/天')

# 计算各区间的RR
print('\n各温度区间相对风险(RR):')
print('-' * 60)
for label in labels:
    mask = merged['temp_bin'] == label
    if mask.sum() > 0:
        avg = merged[mask]['visits'].mean()
        rr = avg / baseline if baseline > 0 else 1
        days = mask.sum()
        print(f'{label:>8}°C: 日均 {avg:5.1f} 人, 天数 {days:3d}, RR = {rr:.3f}')

# 特别分析6-10度区间
print('\n' + '=' * 60)
print('6-10°C 详细分析（当前8°C所在区间）')
print('=' * 60)
mask_6_10 = (merged['tmean'] >= 6) & (merged['tmean'] <= 10)
subset = merged[mask_6_10].copy()
print(f'天数: {len(subset)}')
print(f'日均门诊: {subset["visits"].mean():.2f}')
print(f'门诊范围: {subset["visits"].min()} - {subset["visits"].max()}')
print(f'RR (相对15-20°C): {subset["visits"].mean() / baseline:.3f}')

# 按具体温度看
print('\n逐度分析 (5-12°C):')
for t in range(5, 13):
    mask = (merged['tmean'] >= t) & (merged['tmean'] < t+1)
    if mask.sum() > 0:
        avg = merged[mask]['visits'].mean()
        rr = avg / baseline if baseline > 0 else 1
        print(f'{t}-{t+1}°C: 天数={mask.sum():2d}, 日均={avg:5.1f}, RR={rr:.3f}')

# 整体统计
print('\n' + '=' * 60)
print('整体统计')
print('=' * 60)
print(f'总记录天数: {len(merged)}')
print(f'温度范围: {merged["tmean"].min():.1f} - {merged["tmean"].max():.1f}°C')
print(f'日均门诊整体: {merged["visits"].mean():.2f}')
print(f'门诊量范围: {merged["visits"].min()} - {merged["visits"].max()}')
