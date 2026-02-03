# -*- coding: utf-8 -*-
"""
二分类模型 - 追求最高准确率
呼吸系统疾病 vs 非呼吸系统疾病
"""
import pandas as pd
import numpy as np
from sklearn.model_selection import train_test_split, cross_val_score, StratifiedKFold
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier, AdaBoostClassifier
from sklearn.ensemble import ExtraTreesClassifier, VotingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.svm import SVC
from sklearn.metrics import classification_report, accuracy_score, f1_score, roc_auc_score
import joblib
import json
import time
import warnings
from pathlib import Path
warnings.filterwarnings('ignore')

print("=" * 70)
print("高准确率模型训练 - 二分类策略")
print("=" * 70)

start_time = time.time()

ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_PATH = ROOT_DIR / 'data' / 'research' / '数据.xlsx'
MODELS_DIR = ROOT_DIR / 'models'

# ==================== 1. 加载数据 ====================
print("\n【步骤1】加载数据...")
df = pd.read_excel(DATA_PATH, header=None)
df.columns = ['序号', '医保', '姓名', '性别', '年龄', '就诊时间', 
             '科室', '医生', '疾病分类', '主诉', '病历描述', 
             '列11', '体温', '心率', '血压']

print(f"  原始数据: {len(df)} 条")

# ==================== 2. 特征工程 ====================
print("\n【步骤2】特征工程...")

def parse_age(age_str):
    if pd.isna(age_str):
        return None
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
df['就诊时间'] = pd.to_datetime(df['就诊时间'], errors='coerce')
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

def normalize_gender(value):
    if pd.isna(value):
        return None
    value = str(value).strip()
    if value in ['男', '男性', 'M', 'm', 'male', 'Male']:
        return '男性'
    if value in ['女', '女性', 'F', 'f', 'female', 'Female']:
        return '女性'
    return None

df['性别规范'] = df['性别'].apply(normalize_gender)
df['性别编码'] = df['性别规范'].map({'男性': 1, '女性': 0})

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

# 二分类：呼吸系统 vs 其他
def is_respiratory(disease):
    if pd.isna(disease):
        return None
    disease = str(disease)
    if '呼吸' in disease or '支气管' in disease or '肺' in disease or '咳' in disease:
        return 1  # 呼吸系统疾病
    else:
        return 0  # 非呼吸系统疾病

df['是否呼吸系统'] = df['疾病分类'].apply(is_respiratory)

print("  二分类分布:")
print(f"    呼吸系统疾病: {df['是否呼吸系统'].sum()} ({df['是否呼吸系统'].mean()*100:.1f}%)")
print(f"    非呼吸系统: {len(df) - df['是否呼吸系统'].sum()} ({(1-df['是否呼吸系统'].mean())*100:.1f}%)")

# ==================== 3. 准备数据 ====================
print("\n【步骤3】准备数据...")

feature_cols = ['年龄数值', '性别编码', '月份', '季节', '年龄段', '星期', '小时']

df_clean = df.dropna(subset=feature_cols + ['是否呼吸系统'])
X = df_clean[feature_cols].values
y = df_clean['是否呼吸系统'].values

scaler = StandardScaler()
X_scaled = scaler.fit_transform(X)

X_train, X_test, y_train, y_test = train_test_split(
    X_scaled, y, test_size=0.2, random_state=42, stratify=y
)

print(f"  训练集: {len(X_train)}, 测试集: {len(X_test)}")

# ==================== 4. 训练多个模型 ====================
print("\n【步骤4】训练模型...")

models = {
    'RandomForest': RandomForestClassifier(
        n_estimators=500, max_depth=20, min_samples_split=3,
        class_weight='balanced', random_state=42, n_jobs=-1
    ),
    'GradientBoosting': GradientBoostingClassifier(
        n_estimators=300, max_depth=6, learning_rate=0.05, random_state=42
    ),
    'ExtraTrees': ExtraTreesClassifier(
        n_estimators=500, max_depth=20, class_weight='balanced',
        random_state=42, n_jobs=-1
    ),
    'AdaBoost': AdaBoostClassifier(
        n_estimators=200, learning_rate=0.1, random_state=42
    ),
}

results = {}
best_accuracy = 0
best_model = None
best_name = ""

for name, model in models.items():
    print(f"\n  {name}...")
    
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    cv_scores = cross_val_score(model, X_train, y_train, cv=cv, scoring='accuracy')
    
    model.fit(X_train, y_train)
    y_pred = model.predict(X_test)
    
    accuracy = accuracy_score(y_test, y_pred)
    f1 = f1_score(y_test, y_pred)
    auc = roc_auc_score(y_test, model.predict_proba(X_test)[:, 1])
    
    results[name] = {'accuracy': accuracy, 'f1': f1, 'auc': auc, 'cv': cv_scores.mean()}
    
    print(f"    CV: {cv_scores.mean():.4f}, 测试: {accuracy:.4f}, AUC: {auc:.4f}")
    
    if accuracy > best_accuracy:
        best_accuracy = accuracy
        best_model = model
        best_name = name

# ==================== 5. 集成模型 ====================
print("\n  训练集成模型...")

ensemble = VotingClassifier(
    estimators=[(name, model) for name, model in models.items()],
    voting='soft'
)
ensemble.fit(X_train, y_train)
y_pred_ens = ensemble.predict(X_test)
acc_ens = accuracy_score(y_test, y_pred_ens)
auc_ens = roc_auc_score(y_test, ensemble.predict_proba(X_test)[:, 1])

print(f"    Ensemble 测试: {acc_ens:.4f}, AUC: {auc_ens:.4f}")

if acc_ens > best_accuracy:
    best_accuracy = acc_ens
    best_model = ensemble
    best_name = "Ensemble"

# ==================== 6. 最终结果 ====================
print("\n" + "=" * 70)
print(f"最佳模型: {best_name}")
print(f"测试准确率: {best_accuracy*100:.2f}%")
print("=" * 70)

y_pred = best_model.predict(X_test)
print("\n分类报告:")
print(classification_report(y_test, y_pred, target_names=['非呼吸系统', '呼吸系统']))

# ==================== 7. 保存模型 ====================
print("\n【保存模型】")

# 创建标签编码器
label_encoder = LabelEncoder()
label_encoder.classes_ = np.array(['非呼吸系统疾病', '呼吸系统疾病'])

MODELS_DIR.mkdir(parents=True, exist_ok=True)
joblib.dump(best_model, MODELS_DIR / 'disease_predictor.pkl')
joblib.dump(label_encoder, MODELS_DIR / 'label_encoder.pkl')
joblib.dump(scaler, MODELS_DIR / 'scaler.pkl')

with open(MODELS_DIR / 'feature_config.json', 'w', encoding='utf-8') as f:
    json.dump({
        'feature_cols': feature_cols,
        'classes': ['非呼吸系统疾病', '呼吸系统疾病'],
        'model_name': best_name,
        'accuracy': float(best_accuracy),
        'f1_score': float(f1_score(y_test, y_pred)),
        'model_type': 'binary',
        'description': '高准确率二分类模型'
    }, f, ensure_ascii=False, indent=2)

print("  模型已保存!")

# ==================== 8. 测试 ====================
print("\n【测试预测】")

test_cases = [
    (70, '男', 1),
    (5, '女', 7), 
    (40, '男', 6),
    (35, '女', 4),
]

for age, gender, month in test_cases:
    season = get_season(month)
    age_group = get_age_group(age)
    gender_norm = normalize_gender(gender)
    gender_code = 1 if gender_norm == '男性' else 0
    
    features = np.array([[age, gender_code, month, season, age_group, 3, 10]])
    features_scaled = scaler.transform(features)
    
    pred = best_model.predict(features_scaled)[0]
    prob = best_model.predict_proba(features_scaled)[0]
    
    disease = '呼吸系统疾病' if pred == 1 else '非呼吸系统疾病'
    confidence = prob[pred] * 100
    
    print(f"  {age}岁{gender}性({month}月): {disease} ({confidence:.1f}%)")

total_time = time.time() - start_time
print(f"\n完成！耗时: {total_time:.1f}秒, 准确率: {best_accuracy*100:.2f}%")
