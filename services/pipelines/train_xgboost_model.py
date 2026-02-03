# -*- coding: utf-8 -*-
"""
XGBoost高准确率模型
"""
import pandas as pd
import numpy as np
from sklearn.model_selection import train_test_split, cross_val_score, StratifiedKFold
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.metrics import classification_report, accuracy_score, f1_score
from xgboost import XGBClassifier
import joblib
import json
import time
import warnings
from pathlib import Path
warnings.filterwarnings('ignore')

print("=" * 70)
print("XGBoost 高准确率模型训练")
print("=" * 70)

start_time = time.time()

ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_PATH = ROOT_DIR / 'data' / 'research' / '数据.xlsx'
MODELS_DIR = ROOT_DIR / 'models'

# 加载数据
df = pd.read_excel(DATA_PATH, header=None)
df.columns = ['序号', '医保', '姓名', '性别', '年龄', '就诊时间', 
             '科室', '医生', '疾病分类', '主诉', '病历描述', 
             '列11', '体温', '心率', '血压']

print(f"数据量: {len(df)} 条")

# 特征工程
def parse_age(age_str):
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

df['年龄数值'] = df['年龄'].apply(parse_age)
df['就诊时间'] = pd.to_datetime(df['就诊时间'])
df['月份'] = df['就诊时间'].dt.month
df['星期'] = df['就诊时间'].dt.dayofweek
df['小时'] = df['就诊时间'].dt.hour

def get_season(month):
    if month in [12, 1, 2]:
        return 0
    elif month in [3, 4, 5]:
        return 1
    elif month in [6, 7, 8]:
        return 2
    else:
        return 3

df['季节'] = df['月份'].apply(get_season)
df['性别编码'] = (df['性别'] == '男性').astype(int)

def get_age_group(age):
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

df['年龄段'] = df['年龄数值'].apply(get_age_group)

# 疾病分类
def get_disease_category(disease):
    disease = str(disease)
    if '呼吸' in disease or '支气管' in disease or '肺' in disease or '咳' in disease:
        return '呼吸系统疾病'
    elif '胃' in disease or '肠' in disease or '消化' in disease:
        return '消化系统疾病'
    else:
        return '其他疾病'

df['疾病大类'] = df['疾病分类'].apply(get_disease_category)

print("\n疾病分布:")
for d, c in df['疾病大类'].value_counts().items():
    print(f"  {d}: {c} ({c/len(df)*100:.1f}%)")

# 准备数据
feature_cols = ['年龄数值', '性别编码', '月份', '季节', '年龄段', '星期', '小时']
df_clean = df.dropna(subset=feature_cols)

X = df_clean[feature_cols].values
y_raw = df_clean['疾病大类'].values

label_encoder = LabelEncoder()
y = label_encoder.fit_transform(y_raw)

scaler = StandardScaler()
X_scaled = scaler.fit_transform(X)

X_train, X_test, y_train, y_test = train_test_split(
    X_scaled, y, test_size=0.2, random_state=42, stratify=y
)

print(f"\n训练集: {len(X_train)}, 测试集: {len(X_test)}")
print(f"类别: {list(label_encoder.classes_)}")

# XGBoost模型
print("\n训练XGBoost模型...")

# 计算类别权重
class_counts = np.bincount(y_train)
total = len(y_train)
scale_pos_weight = {i: total / (len(class_counts) * c) for i, c in enumerate(class_counts)}

xgb_model = XGBClassifier(
    n_estimators=500,
    max_depth=8,
    learning_rate=0.05,
    subsample=0.8,
    colsample_bytree=0.8,
    reg_alpha=0.1,
    reg_lambda=1.0,
    random_state=42,
    n_jobs=-1,
    eval_metric='mlogloss'
)

# 交叉验证
cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
cv_scores = cross_val_score(xgb_model, X_train, y_train, cv=cv, scoring='accuracy')
print(f"交叉验证: {cv_scores.mean():.4f} (+/- {cv_scores.std()*2:.4f})")

# 训练
xgb_model.fit(X_train, y_train)

# 评估
y_pred = xgb_model.predict(X_test)
accuracy = accuracy_score(y_test, y_pred)
f1 = f1_score(y_test, y_pred, average='weighted')

print(f"\n测试集准确率: {accuracy*100:.2f}%")
print(f"F1分数: {f1*100:.2f}%")

print("\n分类报告:")
print(classification_report(y_test, y_pred, target_names=label_encoder.classes_))

# 特征重要性
print("\n特征重要性:")
importances = xgb_model.feature_importances_
for feat, imp in sorted(zip(feature_cols, importances), key=lambda x: x[1], reverse=True):
    bar = '█' * int(imp * 30)
    print(f"  {feat:<10}: {imp:.3f} {bar}")

# 保存模型
MODELS_DIR.mkdir(parents=True, exist_ok=True)
joblib.dump(xgb_model, MODELS_DIR / 'disease_predictor.pkl')
joblib.dump(label_encoder, MODELS_DIR / 'label_encoder.pkl')
joblib.dump(scaler, MODELS_DIR / 'scaler.pkl')

with open(MODELS_DIR / 'feature_config.json', 'w', encoding='utf-8') as f:
    json.dump({
        'feature_cols': feature_cols,
        'classes': list(label_encoder.classes_),
        'model_name': 'XGBoost',
        'accuracy': float(accuracy),
        'f1_score': float(f1)
    }, f, ensure_ascii=False, indent=2)

print("\n模型已保存!")

# 测试
print("\n预测示例:")
test_cases = [(70, '男', 1), (5, '女', 7), (45, '男', 12), (60, '女', 3)]
for age, gender, month in test_cases:
    features = np.array([[age, 1 if gender=='男' else 0, month, get_season(month), 
                         get_age_group(age), 3, 10]])
    features_scaled = scaler.transform(features)
    pred = xgb_model.predict(features_scaled)[0]
    prob = xgb_model.predict_proba(features_scaled)[0]
    print(f"  {age}岁{gender}({month}月): {label_encoder.classes_[pred]} ({prob[pred]*100:.1f}%)")

print(f"\n完成! 准确率: {accuracy*100:.2f}%")
