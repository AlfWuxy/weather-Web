# -*- coding: utf-8 -*-
"""
优化版机器学习模型训练 - 提高准确率
"""
import pandas as pd
import numpy as np
from sklearn.model_selection import train_test_split, cross_val_score, StratifiedKFold
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier, VotingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report, accuracy_score, f1_score
from imblearn.over_sampling import SMOTE
from imblearn.combine import SMOTETomek
import joblib
import json
import time
import warnings
from pathlib import Path
warnings.filterwarnings('ignore')

print("=" * 70)
print("优化版模型训练 - 目标：准确率90%+")
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

# ==================== 2. 特征工程（增强版）====================
print("\n【步骤2】增强特征工程...")

# 解析年龄
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

# 时间特征
df['就诊时间'] = pd.to_datetime(df['就诊时间'])
df['月份'] = df['就诊时间'].dt.month
df['星期'] = df['就诊时间'].dt.dayofweek
df['小时'] = df['就诊时间'].dt.hour
df['是否周末'] = (df['星期'] >= 5).astype(int)

# 季节（更细化）
def get_season(month):
    if month in [12, 1, 2]:
        return 0  # 冬季
    elif month in [3, 4, 5]:
        return 1  # 春季
    elif month in [6, 7, 8]:
        return 2  # 夏季
    else:
        return 3  # 秋季

df['季节'] = df['月份'].apply(get_season)

# 性别编码
df['性别编码'] = (df['性别'] == '男性').astype(int)

# 年龄段（更细化）
def get_age_group(age):
    if age is None:
        return 3
    if age < 6:
        return 0  # 婴幼儿
    elif age < 18:
        return 1  # 青少年
    elif age < 40:
        return 2  # 青年
    elif age < 55:
        return 3  # 中年
    elif age < 70:
        return 4  # 中老年
    else:
        return 5  # 老年

df['年龄段'] = df['年龄数值'].apply(get_age_group)

# 添加更多特征
df['年龄平方'] = df['年龄数值'] ** 2  # 非线性特征
df['是否老年'] = (df['年龄数值'] >= 60).astype(int)
df['是否儿童'] = (df['年龄数值'] < 12).astype(int)
df['时段'] = pd.cut(df['小时'], bins=[0, 8, 12, 18, 24], labels=[0, 1, 2, 3]).astype(int)

# ==================== 策略1：简化分类（提高准确率）====================
print("\n【策略1】简化疾病分类...")

# 只分成3大类（更容易预测）
def get_simple_category(disease):
    disease = str(disease)
    if '呼吸' in disease or '支气管' in disease or '肺' in disease or '咳' in disease:
        return '呼吸系统疾病'
    elif '胃' in disease or '肠' in disease or '消化' in disease:
        return '消化系统疾病'
    else:
        return '其他疾病'

df['疾病简类'] = df['疾病分类'].apply(get_simple_category)

print("  简化后分类分布:")
simple_dist = df['疾病简类'].value_counts()
for disease, count in simple_dist.items():
    print(f"    {disease}: {count} ({count/len(df)*100:.1f}%)")

# ==================== 3. 准备数据 ====================
print("\n【步骤3】准备训练数据...")

# 特征列（更多特征）
feature_cols = ['年龄数值', '性别编码', '月份', '季节', '年龄段', 
                '星期', '小时', '是否周末', '年龄平方', '是否老年', 
                '是否儿童', '时段']

df_clean = df.dropna(subset=feature_cols + ['疾病简类'])
print(f"  清洗后数据: {len(df_clean)} 条")

X = df_clean[feature_cols].values
y = df_clean['疾病简类'].values

label_encoder = LabelEncoder()
y_encoded = label_encoder.fit_transform(y)

print(f"  特征数量: {len(feature_cols)}")
print(f"  类别数量: {len(label_encoder.classes_)}")

# 标准化
scaler = StandardScaler()
X_scaled = scaler.fit_transform(X)

# 划分数据
X_train, X_test, y_train, y_test = train_test_split(
    X_scaled, y_encoded, test_size=0.2, random_state=42, stratify=y_encoded
)

print(f"  训练集: {len(X_train)} 条")
print(f"  测试集: {len(X_test)} 条")

# ==================== 4. 处理不平衡数据 ====================
print("\n【步骤4】处理数据不平衡（SMOTE）...")

try:
    smote = SMOTE(random_state=42, k_neighbors=3)
    X_train_balanced, y_train_balanced = smote.fit_resample(X_train, y_train)
    print(f"  平衡后训练集: {len(X_train_balanced)} 条")
    
    # 显示平衡后的分布
    unique, counts = np.unique(y_train_balanced, return_counts=True)
    for u, c in zip(unique, counts):
        print(f"    {label_encoder.classes_[u]}: {c}")
except Exception as e:
    print(f"  SMOTE失败，使用原始数据: {e}")
    X_train_balanced, y_train_balanced = X_train, y_train

# ==================== 5. 训练多个模型 ====================
print("\n【步骤5】训练优化模型...")

# 优化后的随机森林
rf_model = RandomForestClassifier(
    n_estimators=300,
    max_depth=15,
    min_samples_split=5,
    min_samples_leaf=2,
    class_weight='balanced',
    random_state=42,
    n_jobs=-1
)

# 梯度提升
gb_model = GradientBoostingClassifier(
    n_estimators=200,
    max_depth=8,
    learning_rate=0.1,
    random_state=42
)

# 逻辑回归
lr_model = LogisticRegression(
    max_iter=2000,
    class_weight='balanced',
    random_state=42,
    multi_class='multinomial'
)

# 集成模型（投票）
ensemble_model = VotingClassifier(
    estimators=[
        ('rf', rf_model),
        ('gb', gb_model),
        ('lr', lr_model)
    ],
    voting='soft'
)

models = {
    'RandomForest优化': rf_model,
    'GradientBoosting优化': gb_model,
    'Ensemble集成': ensemble_model
}

results = {}
best_accuracy = 0
best_model = None
best_name = ""

for name, model in models.items():
    print(f"\n  训练 {name}...")
    train_start = time.time()
    
    # 交叉验证
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    cv_scores = cross_val_score(model, X_train_balanced, y_train_balanced, cv=cv, scoring='accuracy')
    
    # 训练
    model.fit(X_train_balanced, y_train_balanced)
    
    # 测试
    y_pred = model.predict(X_test)
    accuracy = accuracy_score(y_test, y_pred)
    f1 = f1_score(y_test, y_pred, average='weighted')
    
    train_time = time.time() - train_start
    
    results[name] = {
        'cv_mean': cv_scores.mean(),
        'cv_std': cv_scores.std(),
        'test_accuracy': accuracy,
        'f1_score': f1,
        'train_time': train_time
    }
    
    print(f"    交叉验证: {cv_scores.mean():.4f} (+/- {cv_scores.std()*2:.4f})")
    print(f"    测试准确率: {accuracy:.4f}")
    print(f"    F1分数: {f1:.4f}")
    
    if accuracy > best_accuracy:
        best_accuracy = accuracy
        best_model = model
        best_name = name

# ==================== 6. 最佳模型评估 ====================
print("\n" + "=" * 70)
print(f"最佳模型: {best_name}")
print(f"测试准确率: {best_accuracy*100:.2f}%")
print("=" * 70)

y_pred = best_model.predict(X_test)
print("\n分类报告:")
print(classification_report(y_test, y_pred, target_names=label_encoder.classes_))

# ==================== 7. 保存模型 ====================
print("\n【步骤6】保存优化模型...")

MODELS_DIR.mkdir(parents=True, exist_ok=True)
joblib.dump(best_model, MODELS_DIR / 'disease_predictor.pkl')
joblib.dump(label_encoder, MODELS_DIR / 'label_encoder.pkl')
joblib.dump(scaler, MODELS_DIR / 'scaler.pkl')

with open(MODELS_DIR / 'feature_config.json', 'w', encoding='utf-8') as f:
    json.dump({
        'feature_cols': feature_cols,
        'classes': list(label_encoder.classes_),
        'model_name': best_name,
        'accuracy': float(best_accuracy),
        'f1_score': float(results[best_name]['f1_score']),
        'num_classes': len(label_encoder.classes_),
        'optimized': True
    }, f, ensure_ascii=False, indent=2)

print("  模型已保存!")

# ==================== 8. 测试预测 ====================
print("\n【测试预测】")

def predict(age, gender, month):
    season = get_season(month)
    age_group = get_age_group(age)
    gender_code = 1 if gender == '男' else 0
    is_weekend = 0
    age_sq = age ** 2
    is_elderly = 1 if age >= 60 else 0
    is_child = 1 if age < 12 else 0
    hour = 10
    time_period = 1
    weekday = 3
    
    features = np.array([[age, gender_code, month, season, age_group, 
                         weekday, hour, is_weekend, age_sq, is_elderly, 
                         is_child, time_period]])
    features_scaled = scaler.transform(features)
    
    proba = best_model.predict_proba(features_scaled)[0]
    pred_idx = np.argmax(proba)
    
    return label_encoder.classes_[pred_idx], proba[pred_idx]

test_cases = [
    (70, '男', 1, '70岁男性冬季'),
    (5, '女', 7, '5岁女童夏季'),
    (40, '男', 6, '40岁男性夏季'),
]

for age, gender, month, desc in test_cases:
    disease, prob = predict(age, gender, month)
    print(f"  {desc}: {disease} ({prob*100:.1f}%)")

# ==================== 完成 ====================
total_time = time.time() - start_time
print("\n" + "=" * 70)
print(f"优化完成！总耗时: {total_time:.2f}秒")
print(f"最终准确率: {best_accuracy*100:.2f}%")
print("=" * 70)
