# -*- coding: utf-8 -*-
"""分析数据，为训练预测模型做准备"""
import pandas as pd
import numpy as np
from collections import Counter
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_PATH = ROOT_DIR / 'data' / 'research' / '数据.xlsx'

df = pd.read_excel(DATA_PATH, header=None)
df.columns = ['序号', '医保', '姓名', '性别', '年龄', '就诊时间', '科室', '医生', '疾病分类', '主诉', '病历描述', '列11', '体温', '心率', '血压']

print('=' * 80)
print('数据分析 - 可用于训练预测模型')
print('=' * 80)

print(f'\n总记录数: {len(df)}')

print('\n【疾病分类统计】')
disease_counts = df['疾病分类'].value_counts()
for disease, count in disease_counts.items():
    print(f'  {disease}: {count}条 ({count/len(df)*100:.1f}%)')

print('\n【科室统计】')
dept_counts = df['科室'].value_counts()
for dept, count in dept_counts.items():
    print(f'  {dept}: {count}条')

print('\n【年龄分布】')
def parse_age(age_str):
    """解析年龄字符串"""
    age_str = str(age_str)
    if '岁' in age_str:
        return float(age_str.replace('岁', ''))
    elif '月' in age_str or '天' in age_str:
        return 0  # 婴儿算0岁
    else:
        try:
            return float(age_str)
        except (ValueError, TypeError):
            return None

df['年龄数值'] = df['年龄'].apply(parse_age)
print(f'  最小年龄: {df["年龄数值"].min():.0f}岁')
print(f'  最大年龄: {df["年龄数值"].max():.0f}岁')
print(f'  平均年龄: {df["年龄数值"].mean():.1f}岁')

# 年龄段分布
age_bins = [0, 18, 40, 60, 80, 100]
age_labels = ['0-18岁', '19-40岁', '41-60岁', '61-80岁', '80岁以上']
df['年龄段'] = pd.cut(df['年龄数值'], bins=age_bins, labels=age_labels)
print('\n【年龄段分布】')
for age_group in age_labels:
    count = len(df[df['年龄段'] == age_group])
    print(f'  {age_group}: {count}人 ({count/len(df)*100:.1f}%)')

print('\n【就诊时间范围】')
df['就诊时间'] = pd.to_datetime(df['就诊时间'])
print(f'  最早: {df["就诊时间"].min()}')
print(f'  最晚: {df["就诊时间"].max()}')

print('\n【月份分布】')
df['月份'] = df['就诊时间'].dt.month
month_counts = df['月份'].value_counts().sort_index()
for month, count in month_counts.items():
    print(f'  {month}月: {count}条')

print('\n【性别分布】')
gender_counts = df['性别'].value_counts()
for gender, count in gender_counts.items():
    print(f'  {gender}: {count}人 ({count/len(df)*100:.1f}%)')

print('\n' + '=' * 80)
print('可训练的预测模型:')
print('=' * 80)
print('''
1. 疾病类型预测模型
   输入: 年龄、性别、月份、社区
   输出: 预测可能的疾病类型
   
2. 就诊高峰预测模型
   输入: 月份、社区
   输出: 预测就诊人数

3. 高风险人群识别模型
   输入: 年龄、性别、历史就诊记录
   输出: 患病风险等级
''')
