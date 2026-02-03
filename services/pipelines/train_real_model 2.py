# -*- coding: utf-8 -*-
"""
真正的机器学习模型训练
使用sklearn训练疾病预测模型
"""
import pandas as pd
import numpy as np
from sklearn.model_selection import train_test_split, cross_val_score, GridSearchCV
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.svm import SVC
from sklearn.metrics import classification_report, confusion_matrix, accuracy_score
from sklearn.metrics import precision_score, recall_score, f1_score
import joblib
import warnings
import time
from pathlib import Path
warnings.filterwarnings('ignore')

print("=" * 70)
print("机器学习模型训练 - 天气健康风险预测系统")
print("=" * 70)

# ==================== 1. 数据加载与预处理 ====================
print("\n【步骤1】加载数据...")
start_time = time.time()

ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_PATH = ROOT_DIR / 'data' / 'research' / '数据.xlsx'
MODELS_DIR = ROOT_DIR / 'models'

df = pd.read_excel(DATA_PATH, header=None)
df.columns = ['序号', '医保', '姓名', '性别', '年龄', '就诊时间', 
             '科室', '医生', '疾病分类', '主诉', '病历描述', 
             '列11', '体温', '心率', '血压']

print(f"  原始数据: {len(df)} 条记录")

# ==================== 2. 特征工程 ====================
print("\n【步骤2】特征工程...")

# 2.1 解析年龄
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

# 2.2 解析时间特征
df['就诊时间'] = pd.to_datetime(df['就诊时间'], errors='coerce')
df['月份'] = df['就诊时间'].dt.month
df['星期'] = df['就诊时间'].dt.dayofweek
df['小时'] = df['就诊时间'].dt.hour

# 2.3 季节特征
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

# 2.4 性别编码
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

# 2.5 年龄段
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

# 2.6 疾病大类归并（减少类别数提高准确率）
def get_disease_category(disease):
    if pd.isna(disease):
        return None
    disease = str(disease)
    if '呼吸' in disease or '支气管' in disease or '肺' in disease or '咳' in disease:
        return '呼吸系统疾病'
    elif '胃' in disease or '肠' in disease or '消化' in disease:
        return '消化系统疾病'
    elif '高血压' in disease or '心' in disease or '血管' in disease:
        return '心血管疾病'
    elif '关节' in disease or '骨' in disease or '腰' in disease or '颈' in disease:
        return '骨关节疾病'
    elif '泌尿' in disease or '肾' in disease or '前列腺' in disease:
        return '泌尿系统疾病'
    elif '皮肤' in disease or '感染' in disease:
        return '皮肤感染'
    elif '牙' in disease or '口腔' in disease:
        return '口腔疾病'
    elif '痛风' in disease:
        return '代谢性疾病'
    else:
        return '其他疾病'

df['疾病大类'] = df['疾病分类'].apply(get_disease_category)

print(f"  疾病大类分布:")
disease_dist = df['疾病大类'].value_counts()
for disease, count in disease_dist.items():
    print(f"    {disease}: {count} ({count/len(df)*100:.1f}%)")

# ==================== 3. 准备训练数据 ====================
print("\n【步骤3】准备训练数据...")

# 特征列
feature_cols = ['年龄数值', '性别编码', '月份', '季节', '年龄段', '星期', '小时']

# 移除缺失值
df_clean = df.dropna(subset=feature_cols + ['疾病大类'])
print(f"  清洗后数据: {len(df_clean)} 条记录")

X = df_clean[feature_cols].values
y = df_clean['疾病大类'].values

# 标签编码
label_encoder = LabelEncoder()
y_encoded = label_encoder.fit_transform(y)

print(f"  特征数量: {len(feature_cols)}")
print(f"  类别数量: {len(label_encoder.classes_)}")
print(f"  类别: {list(label_encoder.classes_)}")

# 特征标准化
scaler = StandardScaler()
X_scaled = scaler.fit_transform(X)

# 划分训练集和测试集
X_train, X_test, y_train, y_test = train_test_split(
    X_scaled, y_encoded, test_size=0.2, random_state=42, stratify=y_encoded
)

print(f"  训练集: {len(X_train)} 条")
print(f"  测试集: {len(X_test)} 条")

# ==================== 4. 模型训练与比较 ====================
print("\n【步骤4】训练多个模型并比较...")

models = {
    'RandomForest': RandomForestClassifier(n_estimators=100, random_state=42, n_jobs=-1),
    'GradientBoosting': GradientBoostingClassifier(n_estimators=100, random_state=42),
    'LogisticRegression': LogisticRegression(max_iter=1000, random_state=42, multi_class='multinomial'),
}

results = {}

for name, model in models.items():
    print(f"\n  训练 {name}...")
    train_start = time.time()
    
    # 交叉验证
    cv_scores = cross_val_score(model, X_train, y_train, cv=5, scoring='accuracy')
    
    # 训练模型
    model.fit(X_train, y_train)
    
    # 测试集预测
    y_pred = model.predict(X_test)
    
    # 计算指标
    accuracy = accuracy_score(y_test, y_pred)
    f1 = f1_score(y_test, y_pred, average='weighted')
    
    train_time = time.time() - train_start
    
    results[name] = {
        'model': model,
        'cv_mean': cv_scores.mean(),
        'cv_std': cv_scores.std(),
        'test_accuracy': accuracy,
        'f1_score': f1,
        'train_time': train_time
    }
    
    print(f"    交叉验证准确率: {cv_scores.mean():.4f} (+/- {cv_scores.std()*2:.4f})")
    print(f"    测试集准确率: {accuracy:.4f}")
    print(f"    F1分数: {f1:.4f}")
    print(f"    训练时间: {train_time:.2f}秒")

# ==================== 5. 选择最佳模型 ====================
print("\n【步骤5】选择最佳模型...")

best_model_name = max(results, key=lambda x: results[x]['test_accuracy'])
best_result = results[best_model_name]
best_model = best_result['model']

print(f"\n  最佳模型: {best_model_name}")
print(f"  测试集准确率: {best_result['test_accuracy']:.4f}")
print(f"  F1分数: {best_result['f1_score']:.4f}")

# ==================== 6. 详细评估 ====================
print("\n【步骤6】模型初选完成，准备调优与最终评估...")

# ==================== 7. 特征重要性 ====================
if hasattr(best_model, 'feature_importances_'):
    print("\n【步骤7】特征重要性分析...")
    importances = best_model.feature_importances_
    feature_importance = sorted(zip(feature_cols, importances), key=lambda x: x[1], reverse=True)
    
    print("\n  特征重要性排序:")
    for feature, importance in feature_importance:
        bar = '█' * int(importance * 50)
        print(f"    {feature:<12}: {importance:.4f} {bar}")

# ==================== 8. 超参数调优 ====================
print("\n【步骤8】对最佳模型进行超参数调优...")

if best_model_name == 'RandomForest':
    param_grid = {
        'n_estimators': [100, 200, 300],
        'max_depth': [10, 20, 30, None],
        'min_samples_split': [2, 5, 10],
        'min_samples_leaf': [1, 2, 4]
    }
    
    print("  进行网格搜索（这可能需要几分钟）...")
    grid_search = GridSearchCV(
        RandomForestClassifier(random_state=42, n_jobs=-1),
        param_grid,
        cv=3,
        scoring='accuracy',
        n_jobs=-1,
        verbose=0
    )
    
    grid_search.fit(X_train, y_train)
    
    print(f"\n  最佳参数: {grid_search.best_params_}")
    print(f"  最佳交叉验证分数: {grid_search.best_score_:.4f}")
    
    # 使用最佳参数的模型
    best_model = grid_search.best_estimator_
    y_pred_tuned = best_model.predict(X_test)
    tuned_accuracy = accuracy_score(y_test, y_pred_tuned)
    print(f"  调优后测试集准确率: {tuned_accuracy:.4f}")

# ==================== 9. 最终评估 ====================
print("\n【步骤9】最终评估最佳模型...")

y_pred_final = best_model.predict(X_test)
final_accuracy = accuracy_score(y_test, y_pred_final)
final_f1 = f1_score(y_test, y_pred_final, average='weighted')

print("\n  分类报告:")
print(classification_report(y_test, y_pred_final, target_names=label_encoder.classes_))

# ==================== 10. 保存模型 ====================
print("\n【步骤10】保存模型...")

# 保存模型
MODELS_DIR.mkdir(parents=True, exist_ok=True)
joblib.dump(best_model, MODELS_DIR / 'disease_predictor.pkl')
joblib.dump(label_encoder, MODELS_DIR / 'label_encoder.pkl')
joblib.dump(scaler, MODELS_DIR / 'scaler.pkl')

# 保存特征列名
import json
with open(MODELS_DIR / 'feature_config.json', 'w', encoding='utf-8') as f:
    json.dump({
        'feature_cols': feature_cols,
        'classes': list(label_encoder.classes_),
        'model_name': best_model_name,
        'accuracy': float(final_accuracy),
        'f1_score': float(final_f1)
    }, f, ensure_ascii=False, indent=2)

print(f"  模型已保存到 {MODELS_DIR}/ 目录")

# ==================== 11. 测试预测 ====================
print("\n【步骤11】测试预测示例...")

def predict_disease_risk(age, gender, month):
    """预测疾病风险"""
    season = get_season(month)
    age_group = get_age_group(age)
    gender_norm = normalize_gender(gender)
    gender_code = 1 if gender_norm == '男性' else 0
    
    features = np.array([[age, gender_code, month, season, age_group, 3, 10]])  # 假设周三上午10点
    features_scaled = scaler.transform(features)
    
    # 预测概率
    proba = best_model.predict_proba(features_scaled)[0]
    
    # 获取前3个最可能的疾病
    top_indices = np.argsort(proba)[::-1][:3]
    
    results = []
    for idx in top_indices:
        results.append({
            'disease': label_encoder.classes_[idx],
            'probability': proba[idx]
        })
    
    return results

# 测试案例
test_cases = [
    {'age': 70, 'gender': '男', 'month': 1, 'desc': '70岁男性，1月（冬季）'},
    {'age': 70, 'gender': '女', 'month': 7, 'desc': '70岁女性，7月（夏季）'},
    {'age': 35, 'gender': '男', 'month': 4, 'desc': '35岁男性，4月（春季）'},
    {'age': 5, 'gender': '女', 'month': 12, 'desc': '5岁女童，12月（冬季）'},
]

print("\n  预测示例:")
for case in test_cases:
    print(f"\n  {case['desc']}:")
    predictions = predict_disease_risk(case['age'], case['gender'], case['month'])
    for pred in predictions:
        print(f"    {pred['disease']}: {pred['probability']*100:.1f}%")

# ==================== 完成 ====================
total_time = time.time() - start_time
print("\n" + "=" * 70)
print(f"训练完成！总耗时: {total_time:.2f}秒")
print("=" * 70)

print(f"""
模型性能总结:
  - 最佳模型: {best_model_name}
  - 测试集准确率: {final_accuracy*100:.2f}%
  - F1分数: {final_f1*100:.2f}%
  - 训练样本: {len(X_train)}
  - 测试样本: {len(X_test)}
  - 疾病类别: {len(label_encoder.classes_)}
  
模型文件:
  - {MODELS_DIR}/disease_predictor.pkl
  - {MODELS_DIR}/label_encoder.pkl  
  - {MODELS_DIR}/scaler.pkl
  - {MODELS_DIR}/feature_config.json
""")
