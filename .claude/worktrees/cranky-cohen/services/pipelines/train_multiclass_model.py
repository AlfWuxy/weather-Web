# -*- coding: utf-8 -*-
"""
多分类疾病预测模型训练
包含更多天气因素和多种疾病分类
"""
import pandas as pd
import numpy as np
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.metrics import classification_report, accuracy_score, f1_score
import joblib
import json
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_PATH = ROOT_DIR / 'data' / 'research' / '数据.xlsx'
WEATHER_PATH = ROOT_DIR / 'data' / 'raw' / '逐日数据.csv'
MODELS_DIR = ROOT_DIR / 'models'

def parse_age(age_str):
    """解析年龄字符串"""
    age_str = str(age_str)
    if '岁' in age_str:
        try:
            return float(age_str.replace('岁', ''))
        except (ValueError, TypeError):
            return None
    elif '月' in age_str or '天' in age_str:
        return 0
    else:
        try:
            return float(age_str)
        except (ValueError, TypeError):
            return None

def get_season(month):
    """获取季节"""
    if month in [12, 1, 2]:
        return 0  # 冬季
    elif month in [3, 4, 5]:
        return 1  # 春季
    elif month in [6, 7, 8]:
        return 2  # 夏季
    else:
        return 3  # 秋季

def get_age_group(age):
    """获取年龄段"""
    if age is None:
        return 2
    if age < 18:
        return 0
    elif age < 40:
        return 1
    elif age < 60:
        return 2
    elif age < 80:
        return 3
    else:
        return 4

def train_model():
    print("=" * 60)
    print("训练多分类疾病预测模型")
    print("=" * 60)
    
    # 1. 加载病历数据
    print("\n1. 加载病历数据...")
    df_medical = pd.read_excel(DATA_PATH, header=None)
    df_medical.columns = ['序号', '医保', '姓名', '性别', '年龄', '就诊时间', 
                         '科室', '医生', '疾病分类', '主诉', '病历描述', 
                         '列11', '体温', '心率', '血压']
    
    print(f"   病历记录数: {len(df_medical)}")
    
    # 2. 加载天气数据
    print("\n2. 加载天气数据...")
    df_weather = pd.read_csv(WEATHER_PATH, encoding='utf-8')
    
    # 重命名天气数据列
    weather_cols = {
        '日期': 'date',
        '2米平均气温 (多源融合)(°C)': 'tmean',
        '2米最低气温 (多源融合)(°C)': 'tmin',
        '2米最高气温 (多源融合)(°C)': 'tmax',
        '2米平均体感温度 (多源融合)(°C)': 'feels_like',
        '2米体感温度最低值 (多源融合)(°C)': 'feels_like_min',
        '2米体感温度最高值 (多源融合)(°C)': 'feels_like_max',
        '2米平均相对湿度 (多源融合)(%)': 'humidity',
        '2米最大相对湿度 (多源融合)(%)': 'humidity_max',
        '2米最小相对湿度 (多源融合)(%)': 'humidity_min',
        '10米平均风速 (多源融合)(m/s)': 'wind_speed',
        '10米最大风速 (多源融合)(m/s)': 'wind_speed_max',
        '总降水量(雨+雪) (多源融合)(mm)': 'precipitation',
        '降雨量 (多源融合)(mm)': 'rainfall',
        '日照时数 (多源融合)(s)': 'sunshine_hours',
        '短波辐射总量 (多源融合)(MJ/m²)': 'radiation'
    }
    
    df_weather = df_weather.rename(columns=weather_cols)
    df_weather['date'] = pd.to_datetime(df_weather['date'])
    print(f"   天气记录数: {len(df_weather)}")
    print(f"   日期范围: {df_weather['date'].min()} 至 {df_weather['date'].max()}")
    
    # 3. 处理病历数据
    print("\n3. 处理病历数据...")
    df_medical['年龄数值'] = df_medical['年龄'].apply(parse_age)
    df_medical['就诊时间'] = pd.to_datetime(df_medical['就诊时间'])
    df_medical['就诊日期'] = df_medical['就诊时间'].dt.date
    df_medical['月份'] = df_medical['就诊时间'].dt.month
    df_medical['季节'] = df_medical['月份'].apply(get_season)
    df_medical['年龄段'] = df_medical['年龄数值'].apply(get_age_group)
    df_medical['星期'] = df_medical['就诊时间'].dt.weekday
    df_medical['小时'] = df_medical['就诊时间'].dt.hour
    df_medical['性别编码'] = df_medical['性别'].apply(lambda x: 1 if x in ['男', '男性'] else 0)
    
    # 4. 合并天气数据
    print("\n4. 合并天气数据...")
    df_weather['date'] = df_weather['date'].dt.date
    df_merged = df_medical.merge(
        df_weather[['date', 'tmean', 'tmin', 'tmax', 'feels_like', 
                   'humidity', 'wind_speed', 'precipitation', 'sunshine_hours']],
        left_on='就诊日期',
        right_on='date',
        how='left'
    )
    
    print(f"   合并后记录数: {len(df_merged)}")
    
    # 填充缺失的天气数据
    weather_cols_fill = ['tmean', 'tmin', 'tmax', 'feels_like', 'humidity', 
                         'wind_speed', 'precipitation', 'sunshine_hours']
    for col in weather_cols_fill:
        if col in df_merged.columns:
            df_merged[col] = df_merged[col].fillna(df_merged[col].median())
    
    # 5. 分析疾病分类
    print("\n5. 分析疾病分类...")
    disease_counts = df_merged['疾病分类'].value_counts()
    print(f"   疾病分类数量: {len(disease_counts)}")
    print("\n   各疾病分类样本数:")
    for disease, count in disease_counts.items():
        print(f"     - {disease}: {count}")
    
    # 选择样本数足够的疾病分类（至少10个样本）
    valid_diseases = disease_counts[disease_counts >= 10].index.tolist()
    print(f"\n   有效疾病分类数 (>=10样本): {len(valid_diseases)}")
    
    # 6. 准备训练数据
    print("\n6. 准备训练数据...")
    df_train = df_merged[df_merged['疾病分类'].isin(valid_diseases)].copy()
    
    # 特征列
    feature_cols = [
        '年龄数值', '性别编码', '月份', '季节', '年龄段', '星期', '小时',
        'tmean', 'tmin', 'tmax', 'feels_like', 'humidity', 
        'wind_speed', 'precipitation', 'sunshine_hours'
    ]
    
    # 确保特征列存在并处理缺失值
    for col in feature_cols:
        if col not in df_train.columns:
            df_train[col] = 0
        else:
            df_train[col] = df_train[col].fillna(df_train[col].median() if df_train[col].dtype in ['float64', 'int64'] else 0)
    
    X = df_train[feature_cols].values
    y = df_train['疾病分类'].values
    
    print(f"   特征数量: {len(feature_cols)}")
    print(f"   样本数量: {len(X)}")
    
    # 7. 标签编码
    print("\n7. 标签编码...")
    label_encoder = LabelEncoder()
    y_encoded = label_encoder.fit_transform(y)
    print(f"   疾病类别: {list(label_encoder.classes_)}")
    
    # 8. 标准化
    print("\n8. 特征标准化...")
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    
    # 9. 划分训练集和测试集
    print("\n9. 划分训练集和测试集...")
    X_train, X_test, y_train, y_test = train_test_split(
        X_scaled, y_encoded, test_size=0.2, random_state=42, stratify=y_encoded
    )
    print(f"   训练集: {len(X_train)} 样本")
    print(f"   测试集: {len(X_test)} 样本")
    
    # 10. 训练模型
    print("\n10. 训练多分类模型...")
    
    # 使用随机森林（对多分类效果更好）
    model = RandomForestClassifier(
        n_estimators=200,
        max_depth=15,
        min_samples_split=5,
        min_samples_leaf=2,
        random_state=42,
        n_jobs=-1,
        class_weight='balanced'  # 处理类别不平衡
    )
    
    model.fit(X_train, y_train)
    
    # 11. 评估模型
    print("\n11. 评估模型...")
    y_pred = model.predict(X_test)
    accuracy = accuracy_score(y_test, y_pred)
    f1 = f1_score(y_test, y_pred, average='weighted')
    
    print(f"\n   准确率: {accuracy*100:.2f}%")
    print(f"   F1分数 (加权): {f1*100:.2f}%")
    
    print("\n   分类报告:")
    print(classification_report(y_test, y_pred, target_names=label_encoder.classes_))
    
    # 12. 特征重要性
    print("\n12. 特征重要性:")
    importances = model.feature_importances_
    feature_importance = list(zip(feature_cols, importances))
    feature_importance.sort(key=lambda x: x[1], reverse=True)
    for name, importance in feature_importance:
        print(f"     {name}: {importance*100:.2f}%")
    
    # 13. 保存模型
    print("\n13. 保存模型...")
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    
    joblib.dump(model, MODELS_DIR / 'disease_predictor.pkl')
    joblib.dump(scaler, MODELS_DIR / 'scaler.pkl')
    joblib.dump(label_encoder, MODELS_DIR / 'label_encoder.pkl')
    
    # 保存配置
    config = {
        'feature_cols': feature_cols,
        'classes': list(label_encoder.classes_),
        'model_name': 'RandomForest',
        'accuracy': float(accuracy),
        'f1_score': float(f1),
        'model_type': 'multiclass',
        'description': '多分类疾病预测模型，包含天气因素',
        'weather_features': ['tmean', 'tmin', 'tmax', 'feels_like', 'humidity', 
                            'wind_speed', 'precipitation', 'sunshine_hours'],
        'feature_importance': {name: float(imp) for name, imp in feature_importance}
    }
    
    with open(MODELS_DIR / 'feature_config.json', 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    
    print(f"\n   模型已保存到 {MODELS_DIR}/ 目录")
    
    print("\n" + "=" * 60)
    print("训练完成！")
    print("=" * 60)
    
    return model, scaler, label_encoder, config

if __name__ == '__main__':
    train_model()

