# -*- coding: utf-8 -*-
"""
DLNM风险函数服务 - 分布式滞后非线性模型
实现温度×滞后→RR风险曲面的计算

核心思想：
log(E[Y_t]) = α + cb(Temp_t, lag) + s(time) + DOW + confounders

关键输出：
- MMT/Topt（风险最低温度）
- 累积RR（热端短滞后、冷端长滞后）
- AF/AN（可归因比例/病例数）

更新说明 (2025-01):
- 整合文献参数进行平滑校准
- 添加RR上限防止小样本极端值
- 基于多中心研究的年龄/病种修正系数
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from scipy import stats
from scipy.interpolate import UnivariateSpline
from collections import defaultdict
import json
from pathlib import Path


# ============================================================
# 文献参数 - 基于中国南方亚热带地区多中心研究
# 主要来源: Zeng2016, Huang2015, Yang2012, Wu2025, Zhao2019
# ============================================================
LITERATURE_PRIORS = {
    # MMT先验 (江西都昌县，中部亚热带)
    'mmt': {
        'range': (20.0, 26.0),  # 可接受范围
        'typical': 23.0,        # 典型值
        'morbidity_respiratory': 15.2,  # 呼吸系统门诊最适温度
    },

    # 冷效应RR (累积滞后)
    'cold_rr': {
        'p1': {'typical': 1.60, 'range': (1.35, 1.90)},   # 极端冷
        'p5': {'typical': 1.45, 'range': (1.25, 1.70)},
        'p10': {'typical': 1.35, 'range': (1.20, 1.55)},  # 中度冷
        'p25': {'typical': 1.20, 'range': (1.10, 1.35)},
    },

    # 热效应RR (累积滞后)
    'heat_rr': {
        'p75': {'typical': 1.02, 'range': (1.00, 1.06)},
        'p90': {'typical': 1.06, 'range': (1.02, 1.10)},  # 中度热
        'p95': {'typical': 1.08, 'range': (1.03, 1.15)},
        'p99': {'typical': 1.15, 'range': (1.09, 1.22)},  # 极端热
    },

    # RR上限 (防止小样本极端值)
    'rr_caps': {
        'single_day_morbidity': 2.2,  # 单日门诊RR上限
        'single_day_mortality': 1.8,  # 单日死亡RR上限
        'cumulative_morbidity': 3.5,  # 累积门诊RR上限
        'cumulative_mortality': 3.0,  # 累积死亡RR上限
    },

    # 滞后权重 (文献标准化权重)
    'lag_weights': {
        'heat': [0.40, 0.25, 0.15, 0.10, 0.05, 0.03, 0.015, 0.005],  # lag 0-7, 热效应前置
        'cold': [0.03, 0.05, 0.07, 0.09, 0.10, 0.10, 0.10, 0.09,
                 0.08, 0.07, 0.06, 0.05, 0.04, 0.03, 0.04],  # lag 0-14, 冷效应延迟
    },

    # 年龄修正系数 (相对18-64岁基线)
    'age_modifiers': {
        '60-74': {'cold': 1.37, 'heat': 1.18},
        '75+': {'cold': 1.54, 'heat': 2.09},
    },

    # 病种专项RR (基于广州、东莞等研究)
    'disease_rr': {
        'respiratory': {
            'cold_rr': 1.35,  # p1 vs p10, 广州
            'heat_rr': 1.30,  # p99 vs p90
            'cold_sensitivity': 1.5,
            'heat_sensitivity': 1.3,
            'optimal_temp': 15.2,
        },
        'cardiovascular': {
            'cold_rr': 1.24,  # CVD
            'heat_rr': 1.21,
            'hot_night_rr': 1.34,  # HNE
            'cold_sensitivity': 1.3,
            'heat_sensitivity': 1.4,
            'hot_night_sensitivity': 1.6,
        },
        'digestive': {
            'heat_rr': 1.15,  # 菌痢/腹泻
            'cold_sensitivity': 0.9,
            'heat_sensitivity': 1.4,
        },
    },

    # 热夜阈值
    'hot_night': {
        'threshold_percentile': 90,  # NH90th
        'fixed_threshold': 25.0,     # 热带夜定义
    },
}


class DLNMRiskService:
    """DLNM分布式滞后非线性模型服务

    整合本地数据训练与文献先验的混合模型。
    当本地样本量不足时，自动向文献先验靠拢。
    """

    def __init__(self, literature_weight=0.5):
        """
        初始化DLNM服务

        参数:
        - literature_weight: 文献先验权重 (0-1)
          0 = 完全使用本地数据
          1 = 完全使用文献参数
          0.5 = 平衡混合 (默认，推荐用于小样本)
        """
        self.model_trained = False
        self.risk_surface = None  # 温度-滞后风险曲面
        self.mmt = None  # 最低死亡温度 (Minimum Mortality Temperature)
        self.temperature_rr = {}  # 温度-RR映射
        self.lag_weights = {}  # 滞后权重
        self.percentiles = {}  # 温度分位数
        self.disease_specific_rr = {}  # 病种专项RR
        self.seasonal_baseline = {}  # 季节基线
        # hot night threshold (optional)
        self.tmin_p90 = None

        # 模型参数
        self.max_lag = 7  # 最大滞后天数 (热效应)
        self.max_lag_cold = 14  # 冷效应最大滞后
        self.temp_knots = 5  # 温度样条节点数
        self.lag_knots = 3  # 滞后样条节点数

        # 文献先验权重 (用于平滑小样本估计)
        self.literature_weight = literature_weight
        self.literature_priors = LITERATURE_PRIORS

        # RR上限
        self.rr_cap_single = LITERATURE_PRIORS['rr_caps']['single_day_morbidity']
        self.rr_cap_cumulative = LITERATURE_PRIORS['rr_caps']['cumulative_morbidity']

        # 本地数据样本量记录 (用于动态调整权重)
        self.sample_counts = {}

        # 加载并训练模型
        self._load_weather_data()
        self._load_medical_data()
        self._train_model()

    def _load_weather_data(self):
        """加载逐日天气数据"""
        try:
            base_dir = Path(__file__).resolve().parents[1]
            weather_path = base_dir / 'data' / 'raw' / '逐日数据.csv'
            self.weather_df = pd.read_csv(weather_path, encoding='utf-8')
            
            # 解析列名（处理中文和特殊字符）
            self.weather_df.columns = [col.strip() for col in self.weather_df.columns]
            
            # 标准化列名
            column_mapping = {
                '日期': 'date',
                '2米平均气温 (多源融合)(°C)': 'tmean',
                '2米最低气温 (多源融合)(°C)': 'tmin',
                '2米最高气温 (多源融合)(°C)': 'tmax',
                '2米平均相对湿度 (多源融合)(%)': 'humidity',
                '降雨量 (多源融合)(mm)': 'precipitation',
                '10米平均风速 (多源融合)(m/s)': 'wind_speed',
                '2米平均体感温度 (多源融合)(°C)': 'apparent_temp'
            }
            
            # 尝试匹配列名
            for old_col, new_col in column_mapping.items():
                matching_cols = [c for c in self.weather_df.columns if old_col in c]
                if matching_cols:
                    self.weather_df = self.weather_df.rename(columns={matching_cols[0]: new_col})
            
            # 转换日期
            if '日期' in self.weather_df.columns:
                self.weather_df['date'] = pd.to_datetime(self.weather_df['日期'])
            elif 'date' not in self.weather_df.columns:
                # 尝试查找日期列
                for col in self.weather_df.columns:
                    if '日期' in col or 'date' in col.lower():
                        self.weather_df['date'] = pd.to_datetime(self.weather_df[col])
                        break
            
            # 确保有温度数据
            if 'tmean' not in self.weather_df.columns:
                # 尝试查找温度列
                for col in self.weather_df.columns:
                    if '平均气温' in col:
                        self.weather_df['tmean'] = pd.to_numeric(self.weather_df[col], errors='coerce')
                        break
                    elif '2米平均气温' in col:
                        self.weather_df['tmean'] = pd.to_numeric(self.weather_df[col], errors='coerce')
                        break
            
            print(f"✅ 天气数据加载成功: {len(self.weather_df)} 天记录")
            print(f"   日期范围: {self.weather_df['date'].min()} 至 {self.weather_df['date'].max()}")
            
        except Exception as e:
            print(f"⚠️ 天气数据加载失败: {e}")
            self.weather_df = pd.DataFrame()
    
    def _load_medical_data(self):
        """加载病历数据"""
        try:
            base_dir = Path(__file__).resolve().parents[1]
            data_path = base_dir / 'data' / 'research' / '数据.xlsx'
            self.medical_df = pd.read_excel(data_path, header=None)
            self.medical_df.columns = ['序号', '医保', '姓名', '性别', '年龄', '就诊时间', 
                                       '科室', '医生', '疾病分类', '主诉', '病历描述', 
                                       '列11', '体温', '心率', '血压']
            
            # 解析时间和年龄
            self.medical_df['就诊时间'] = pd.to_datetime(self.medical_df['就诊时间'])
            self.medical_df['年龄数值'] = self.medical_df['年龄'].apply(self._parse_age)
            self.medical_df['date'] = self.medical_df['就诊时间'].dt.date
            
            # 按日期统计门诊量
            self.daily_visits = self.medical_df.groupby('date').size().reset_index(name='visits')
            self.daily_visits['date'] = pd.to_datetime(self.daily_visits['date'])
            
            # 老年人门诊（≥60岁）
            elderly_df = self.medical_df[self.medical_df['年龄数值'] >= 60]
            self.elderly_daily_visits = elderly_df.groupby('date').size().reset_index(name='visits')
            self.elderly_daily_visits['date'] = pd.to_datetime(self.elderly_daily_visits['date'])
            
            print(f"✅ 病历数据加载成功: {len(self.medical_df)} 条记录")
            
        except Exception as e:
            print(f"⚠️ 病历数据加载失败: {e}")
            import traceback
            traceback.print_exc()
            self.medical_df = pd.DataFrame()
            self.daily_visits = pd.DataFrame()
            self.elderly_daily_visits = pd.DataFrame()
    
    def _parse_age(self, age_str):
        """解析年龄"""
        age_str = str(age_str)
        if '岁' in age_str:
            try:
                return float(age_str.replace('岁', ''))
            except (ValueError, TypeError):
                return None
        elif '月' in age_str or '天' in age_str:
            return 0
        try:
            return float(age_str)
        except (ValueError, TypeError):
            return None
    
    def _train_model(self):
        """训练DLNM风险函数模型"""
        if self.weather_df.empty or self.daily_visits.empty:
            print("⚠️ 数据不足，无法训练模型")
            return
        
        try:
            # 合并天气和门诊数据
            merged_df = self._merge_data()
            
            if merged_df.empty or len(merged_df) < 30:
                print("⚠️ 合并后数据不足")
                return
            
            # 1. 计算温度分位数和MMT
            self._calculate_temperature_percentiles(merged_df)
            
            # 2. 构建温度-滞后-RR风险曲面
            self._build_risk_surface(merged_df)
            
            # 3. 计算病种专项RR
            self._calculate_disease_specific_rr()
            
            # 4. 计算季节基线
            self._calculate_seasonal_baseline(merged_df)
            
            self.model_trained = True
            print("✅ DLNM风险模型训练完成")
            print(f"   MMT (最低风险温度): {self.mmt:.1f}°C")
            print(f"   温度范围: {self.percentiles.get('p5', 0):.1f}°C - {self.percentiles.get('p95', 35):.1f}°C")
            
        except Exception as e:
            print(f"⚠️ 模型训练失败: {e}")
            import traceback
            traceback.print_exc()
    
    def _merge_data(self):
        """合并天气和门诊数据，并创建滞后变量"""
        try:
            # 确保日期格式一致
            weather_temp = self.weather_df.copy()
            visits_temp = self.daily_visits.copy()
            
            # 安全转换日期
            try:
                weather_temp['date'] = pd.to_datetime(weather_temp['date']).dt.date
            except Exception:
                weather_temp['date'] = pd.to_datetime(weather_temp['date'], errors='coerce').dt.date
            
            try:
                visits_temp['date'] = pd.to_datetime(visits_temp['date']).dt.date
            except Exception:
                visits_temp['date'] = pd.to_datetime(visits_temp['date'], errors='coerce').dt.date
            
            # 合并
            merged = pd.merge(visits_temp, weather_temp, on='date', how='inner')
            # IMPORTANT: lag features depend on row order
            merged = merged.sort_values('date').reset_index(drop=True)
            
            if merged.empty:
                print("⚠️ 合并后数据为空")
                return pd.DataFrame()
            
            # 确保 tmean 列为数值类型
            if 'tmean' in merged.columns:
                merged['tmean'] = pd.to_numeric(merged['tmean'], errors='coerce')
            else:
                print("⚠️ 缺少 tmean 列")
                return pd.DataFrame()
            
            # 创建滞后变量（lag 0-7）
            for lag in range(self.max_lag + 1):
                merged[f'tmean_lag{lag}'] = merged['tmean'].shift(lag)
                if 'tmin' in merged.columns:
                    merged['tmin'] = pd.to_numeric(merged['tmin'], errors='coerce')
                    merged[f'tmin_lag{lag}'] = merged['tmin'].shift(lag)
            
            # 添加时间变量
            merged['date_dt'] = pd.to_datetime(merged['date'])
            merged['dow'] = merged['date_dt'].dt.dayofweek
            merged['month'] = merged['date_dt'].dt.month
            merged['year'] = merged['date_dt'].dt.year
            merged['day_of_year'] = merged['date_dt'].dt.dayofyear
            
            # 计算热夜指标（Tmin >= 阈值）
            if 'tmin' in merged.columns and merged['tmin'].notna().sum() > 0:
                tmin_values = merged['tmin'].dropna()
                if len(tmin_values) > 0:
                    tmin_p90 = tmin_values.quantile(0.9)
                    self.tmin_p90 = float(tmin_p90)
                    merged['hot_night'] = (merged['tmin'] >= tmin_p90).fillna(False).astype(int)
                    # 热夜累积效应 (HNE)
                    merged['hne'] = merged['tmin'].apply(
                        lambda x: max(0, x - tmin_p90) if pd.notna(x) else 0
                    )
                else:
                    merged['hot_night'] = 0
                    merged['hne'] = 0
            else:
                merged['hot_night'] = 0
                merged['hne'] = 0
            
            # 删除缺失值
            required_cols = ['tmean', 'visits'] + [f'tmean_lag{i}' for i in range(self.max_lag + 1)]
            existing_cols = [c for c in required_cols if c in merged.columns]
            merged = merged.dropna(subset=existing_cols)
            
            return merged
            
        except Exception as e:
            print(f"数据合并失败: {e}")
            import traceback
            traceback.print_exc()
            return pd.DataFrame()
    
    def _calculate_temperature_percentiles(self, merged_df):
        """计算温度分位数和MMT，整合文献先验"""
        temps = merged_df['tmean'].dropna()

        # 计算分位数
        self.percentiles = {
            'p1': temps.quantile(0.01),
            'p5': temps.quantile(0.05),
            'p10': temps.quantile(0.10),
            'p25': temps.quantile(0.25),
            'p50': temps.quantile(0.50),
            'p75': temps.quantile(0.75),
            'p90': temps.quantile(0.90),
            'p95': temps.quantile(0.95),
            'p99': temps.quantile(0.99),
            'min': temps.min(),
            'max': temps.max(),
            'mean': temps.mean()
        }

        # 估算本地MMT（最低死亡/发病温度）
        local_mmt = None
        temp_bins = pd.cut(merged_df['tmean'], bins=20)
        avg_visits_by_temp = merged_df.groupby(temp_bins, observed=False)['visits'].mean()

        if not avg_visits_by_temp.empty:
            min_visits_bin = avg_visits_by_temp.idxmin()
            if min_visits_bin is not None:
                local_mmt = (min_visits_bin.left + min_visits_bin.right) / 2

        # 文献MMT先验
        lit_mmt = self.literature_priors['mmt']['typical']
        mmt_range = self.literature_priors['mmt']['range']

        # 混合估计: 本地数据 + 文献先验
        if local_mmt is not None:
            # 检查本地MMT是否在合理范围内
            if mmt_range[0] <= local_mmt <= mmt_range[1]:
                # 本地估计合理，按权重混合
                self.mmt = (1 - self.literature_weight) * local_mmt + self.literature_weight * lit_mmt
            else:
                # 本地估计超出范围，更偏向文献值
                print(f"   ⚠️ 本地MMT({local_mmt:.1f}°C)超出文献范围{mmt_range}，向文献值校正")
                self.mmt = 0.3 * local_mmt + 0.7 * lit_mmt
                # 限制在合理范围
                self.mmt = max(mmt_range[0], min(self.mmt, mmt_range[1]))
        else:
            self.mmt = lit_mmt

        # 记录原始本地MMT用于诊断
        self._local_mmt = local_mmt
    
    def _build_risk_surface(self, merged_df):
        """构建温度-滞后-RR风险曲面"""
        # 使用简化的DLNM方法
        # 计算每个温度区间和滞后天数的相对风险

        temp_bins = np.linspace(self.percentiles['p5'], self.percentiles['p95'], 20)

        # 初始化风险曲面
        self.risk_surface = np.ones((len(temp_bins) - 1, self.max_lag + 1))

        # 计算基准（MMT区间）的门诊量
        mmt_mask = (merged_df['tmean'] >= self.mmt - 2) & (merged_df['tmean'] <= self.mmt + 2)
        baseline_visits = merged_df[mmt_mask]['visits'].mean() if mmt_mask.any() else merged_df['visits'].mean()

        # 记录MMT区间样本量
        self._mmt_sample_count = mmt_mask.sum()

        # 计算每个温度-滞后组合的RR
        for i in range(len(temp_bins) - 1):
            temp_low, temp_high = temp_bins[i], temp_bins[i+1]
            temp_center = (temp_low + temp_high) / 2

            # 记录该温度区间的样本量
            temp_mask = (merged_df['tmean'] >= temp_low) & (merged_df['tmean'] < temp_high)
            self.sample_counts[round(temp_center)] = int(temp_mask.sum())

            for lag in range(self.max_lag + 1):
                lag_col = f'tmean_lag{lag}'
                if lag_col in merged_df.columns:
                    mask = (merged_df[lag_col] >= temp_low) & (merged_df[lag_col] < temp_high)
                    if mask.sum() > 5:
                        lag_visits = merged_df[mask]['visits'].mean()
                        rr = lag_visits / baseline_visits if baseline_visits > 0 else 1.0
                        # 使用文献RR上限
                        self.risk_surface[i, lag] = max(0.5, min(rr, self.rr_cap_single))
                    else:
                        # 样本不足时使用文献先验
                        self.risk_surface[i, lag] = self._get_literature_rr(temp_center)

        # 计算累积RR
        self._calculate_cumulative_rr(temp_bins)

        # 计算滞后权重 (使用文献值)
        self._calculate_lag_weights(merged_df)
    
    def _calculate_cumulative_rr(self, temp_bins):
        """计算累积相对风险，整合文献先验平滑"""
        self.cumulative_rr = {}

        for i in range(len(temp_bins) - 1):
            temp_center = (temp_bins[i] + temp_bins[i+1]) / 2

            # 热效应：短滞后（lag 0-3）
            if temp_center > self.mmt:
                heat_rr = np.mean(self.risk_surface[i, 0:4])  # lag 0-3
                self.cumulative_rr[f'heat_{temp_center:.0f}'] = heat_rr

            # 冷效应：长滞后（lag 0-7）
            elif temp_center < self.mmt:
                cold_rr = np.mean(self.risk_surface[i, :])  # lag 0-7 全部
                self.cumulative_rr[f'cold_{temp_center:.0f}'] = cold_rr

        # 存储温度-RR映射 (结合文献先验平滑)
        for i in range(len(temp_bins) - 1):
            temp_center = (temp_bins[i] + temp_bins[i+1]) / 2
            local_rr = np.mean(self.risk_surface[i, :])

            # 获取该温度区间的样本量
            sample_count = self.sample_counts.get(round(temp_center), 10)

            # 获取文献RR先验
            lit_rr = self._get_literature_rr(temp_center)

            # 动态权重: 样本量越小，越依赖文献
            # 至少30天数据才能完全信任本地估计
            data_confidence = min(sample_count / 30.0, 1.0)
            dynamic_weight = self.literature_weight + (1 - self.literature_weight) * (1 - data_confidence)

            # 混合估计
            blended_rr = (1 - dynamic_weight) * local_rr + dynamic_weight * lit_rr

            # 应用RR上限
            blended_rr = min(blended_rr, self.rr_cap_single)

            self.temperature_rr[round(temp_center, 1)] = blended_rr

    def _get_literature_rr(self, temperature):
        """根据温度获取文献RR先验值"""
        mmt = self.mmt if self.mmt else 23.0

        if temperature >= mmt:
            # 热效应 - 基于文献分位数RR
            heat_priors = self.literature_priors['heat_rr']
            p75 = self.percentiles.get('p75', 25)
            p90 = self.percentiles.get('p90', 30)
            p95 = self.percentiles.get('p95', 33)
            p99 = self.percentiles.get('p99', 36)

            if temperature >= p99:
                return heat_priors['p99']['typical']
            elif temperature >= p95:
                return heat_priors['p95']['typical']
            elif temperature >= p90:
                return heat_priors['p90']['typical']
            elif temperature >= p75:
                return heat_priors['p75']['typical']
            else:
                # MMT到p75之间，线性插值
                return 1.0 + (heat_priors['p75']['typical'] - 1.0) * (temperature - mmt) / max(p75 - mmt, 1)
        else:
            # 冷效应 - 基于文献分位数RR
            cold_priors = self.literature_priors['cold_rr']
            p1 = self.percentiles.get('p1', 0)
            p5 = self.percentiles.get('p5', 3)
            p10 = self.percentiles.get('p10', 6)
            p25 = self.percentiles.get('p25', 12)

            if temperature <= p1:
                return cold_priors['p1']['typical']
            elif temperature <= p5:
                # p1到p5之间插值
                ratio = (p5 - temperature) / max(p5 - p1, 1)
                return cold_priors['p5']['typical'] + ratio * (cold_priors['p1']['typical'] - cold_priors['p5']['typical'])
            elif temperature <= p10:
                ratio = (p10 - temperature) / max(p10 - p5, 1)
                return cold_priors['p10']['typical'] + ratio * (cold_priors['p5']['typical'] - cold_priors['p10']['typical'])
            elif temperature <= p25:
                ratio = (p25 - temperature) / max(p25 - p10, 1)
                return cold_priors['p25']['typical'] + ratio * (cold_priors['p10']['typical'] - cold_priors['p25']['typical'])
            else:
                # p25到MMT之间，线性下降到1.0
                return 1.0 + (cold_priors['p25']['typical'] - 1.0) * (mmt - temperature) / max(mmt - p25, 1)
    
    def _calculate_lag_weights(self, merged_df):
        """使用文献标准化滞后权重"""
        # 热效应：滞后集中在0-3天 (来自文献: 热效应前置, lag0达峰)
        heat_weights = self.literature_priors['lag_weights']['heat']
        self.lag_weights['heat'] = {i: w for i, w in enumerate(heat_weights[:self.max_lag + 1])}

        # 冷效应：滞后更长，分布更均匀 (来自文献: 冷效应延迟, lag3-6达峰)
        cold_weights = self.literature_priors['lag_weights']['cold']
        self.lag_weights['cold'] = {i: w for i, w in enumerate(cold_weights[:self.max_lag + 1])}
    
    def _calculate_disease_specific_rr(self):
        """计算病种专项相对风险 (整合文献参数)"""
        if self.medical_df.empty:
            # 使用纯文献参数
            self._set_literature_disease_rr()
            return

        try:
            # 呼吸系统疾病
            resp_df = self.medical_df[self.medical_df['疾病分类'].str.contains('呼吸|肺|支气管', na=False)]
            # 心脑血管疾病
            cardio_df = self.medical_df[self.medical_df['疾病分类'].str.contains('心|血管|高血压|冠心病', na=False)]
            # 消化系统疾病
            digest_df = self.medical_df[self.medical_df['疾病分类'].str.contains('消化|胃|肠', na=False)]

            # 文献病种参数
            lit_disease = self.literature_priors['disease_rr']
            lit_age = self.literature_priors['age_modifiers']

            # 呼吸系统：冷敏感，滞后更长 (文献: 广州/东莞研究)
            self.disease_specific_rr['respiratory'] = {
                'name': '呼吸系统疾病',
                'cold_sensitivity': lit_disease['respiratory']['cold_sensitivity'],
                'heat_sensitivity': lit_disease['respiratory']['heat_sensitivity'],
                'cold_rr': lit_disease['respiratory']['cold_rr'],
                'heat_rr': lit_disease['respiratory']['heat_rr'],
                'lag_profile': 'cold',
                'case_count': len(resp_df),
                'high_risk_temps': {'cold': True, 'heat': True},
                'optimal_temp_range': (15, 20),
                'optimal_temp': lit_disease['respiratory']['optimal_temp'],
                'age_modifier': self._create_age_modifier('cold')
            }

            # 心脑血管：热夜/复合热更敏感 (文献: 全国热夜研究)
            self.disease_specific_rr['cardiovascular'] = {
                'name': '心脑血管疾病',
                'cold_sensitivity': lit_disease['cardiovascular']['cold_sensitivity'],
                'heat_sensitivity': lit_disease['cardiovascular']['heat_sensitivity'],
                'hot_night_sensitivity': lit_disease['cardiovascular']['hot_night_sensitivity'],
                'cold_rr': lit_disease['cardiovascular']['cold_rr'],
                'heat_rr': lit_disease['cardiovascular']['heat_rr'],
                'hot_night_rr': lit_disease['cardiovascular']['hot_night_rr'],
                'lag_profile': 'heat',
                'case_count': len(cardio_df),
                'high_risk_temps': {'cold': True, 'heat': True},
                'optimal_temp_range': (18, 26),
                'age_modifier': self._create_age_modifier('heat')
            }

            # 消化系统：高温敏感 (文献: 菌痢/腹泻研究)
            self.disease_specific_rr['digestive'] = {
                'name': '消化系统疾病',
                'cold_sensitivity': lit_disease['digestive']['cold_sensitivity'],
                'heat_sensitivity': lit_disease['digestive']['heat_sensitivity'],
                'heat_rr': lit_disease['digestive']['heat_rr'],
                'lag_profile': 'heat',
                'case_count': len(digest_df),
                'high_risk_temps': {'cold': False, 'heat': True},
                'optimal_temp_range': (16, 28),
                'age_modifier': self._create_age_modifier('heat')
            }

            print(f"   病种RR计算完成: 呼吸系统({len(resp_df)}例), 心脑血管({len(cardio_df)}例), 消化系统({len(digest_df)}例)")

        except Exception as e:
            print(f"病种RR计算失败: {e}，使用文献参数")
            self._set_literature_disease_rr()

    def _set_literature_disease_rr(self):
        """使用纯文献参数设置病种RR"""
        lit_disease = self.literature_priors['disease_rr']

        self.disease_specific_rr['respiratory'] = {
            'name': '呼吸系统疾病',
            'cold_sensitivity': lit_disease['respiratory']['cold_sensitivity'],
            'heat_sensitivity': lit_disease['respiratory']['heat_sensitivity'],
            'lag_profile': 'cold',
            'age_modifier': self._create_age_modifier('cold')
        }
        self.disease_specific_rr['cardiovascular'] = {
            'name': '心脑血管疾病',
            'cold_sensitivity': lit_disease['cardiovascular']['cold_sensitivity'],
            'heat_sensitivity': lit_disease['cardiovascular']['heat_sensitivity'],
            'hot_night_sensitivity': lit_disease['cardiovascular']['hot_night_sensitivity'],
            'lag_profile': 'heat',
            'age_modifier': self._create_age_modifier('heat')
        }
        self.disease_specific_rr['digestive'] = {
            'name': '消化系统疾病',
            'cold_sensitivity': lit_disease['digestive']['cold_sensitivity'],
            'heat_sensitivity': lit_disease['digestive']['heat_sensitivity'],
            'lag_profile': 'heat',
            'age_modifier': self._create_age_modifier('heat')
        }

    def _create_age_modifier(self, effect_type):
        """创建基于文献的年龄修正函数

        参数:
        - effect_type: 'cold' 或 'heat'
        """
        lit_age = self.literature_priors['age_modifiers']

        def modifier(age):
            try:
                age = int(age)
                if age >= 75:
                    return lit_age['75+'][effect_type]
                elif age >= 60:
                    return lit_age['60-74'][effect_type]
                else:
                    return 1.0
            except (TypeError, ValueError):
                return 1.0

        return modifier
    
    def _calculate_seasonal_baseline(self, merged_df):
        """计算季节基线"""
        # 按月份计算平均门诊量
        monthly_avg = merged_df.groupby('month')['visits'].mean()
        overall_avg = merged_df['visits'].mean()
        
        for month in range(1, 13):
            if month in monthly_avg.index:
                self.seasonal_baseline[month] = {
                    'avg_visits': monthly_avg[month],
                    'seasonal_factor': monthly_avg[month] / overall_avg if overall_avg > 0 else 1.0
                }
            else:
                self.seasonal_baseline[month] = {
                    'avg_visits': overall_avg,
                    'seasonal_factor': 1.0
                }
    
    def calculate_rr(self, temperature, lag_temperatures=None, disease_type=None, age=None):
        """
        计算给定温度的相对风险
        
        参数:
        - temperature: 当天温度
        - lag_temperatures: 过去7天的温度列表 [lag0, lag1, ..., lag7]
        - disease_type: 疾病类型 ('respiratory', 'cardiovascular', 'digestive', None)
        - age: 年龄（用于年龄修正）
        
        返回:
        - rr: 相对风险值
        - breakdown: 详细分解
        """
        # 确保温度为数值类型
        try:
            temperature = float(temperature) if temperature is not None else 20.0
        except (TypeError, ValueError):
            temperature = 20.0
        
        # 如果模型未训练，使用简化公式
        if not self.model_trained:
            # 简化RR计算：偏离20度越多，风险越高
            deviation = abs(temperature - 20)
            rr = 1.0 + 0.015 * deviation
            return rr, {'error': '模型未训练，使用简化公式', 'base_rr': rr}
        
        # 获取MMT，如果未计算则使用默认值
        mmt = self.mmt if self.mmt is not None else 20.0
        
        # 基础RR计算
        rr = self._get_base_rr(temperature)
        
        # 考虑滞后效应
        if lag_temperatures is not None and len(lag_temperatures) > 0:
            # 确保滞后温度都是数值
            clean_lag_temps = []
            for t in lag_temperatures:
                try:
                    clean_lag_temps.append(float(t) if t is not None else temperature)
                except (TypeError, ValueError):
                    clean_lag_temps.append(temperature)
            if clean_lag_temps:
                rr = self._apply_lag_effects(temperature, clean_lag_temps, rr)
        
        # 病种专项调整
        disease_modifier = 1.0
        if disease_type and disease_type in self.disease_specific_rr:
            disease_info = self.disease_specific_rr[disease_type]
            
            # 根据温度判断使用哪个敏感系数
            if temperature < mmt:
                disease_modifier = disease_info.get('cold_sensitivity', 1.0)
            else:
                disease_modifier = disease_info.get('heat_sensitivity', 1.0)
        
        # 年龄修正 (使用文献参数)
        age_modifier = 1.0
        if age is not None:
            try:
                age = int(age)
                # 确定使用冷效应还是热效应的年龄修正
                effect_type = 'cold' if temperature < mmt else 'heat'

                if disease_type and disease_type in self.disease_specific_rr:
                    age_mod_func = self.disease_specific_rr[disease_type].get('age_modifier')
                    if callable(age_mod_func):
                        age_modifier = age_mod_func(age)
                else:
                    # 通用年龄修正 (基于文献)
                    lit_age = self.literature_priors['age_modifiers']
                    if age >= 75:
                        age_modifier = lit_age['75+'][effect_type]
                    elif age >= 60:
                        age_modifier = lit_age['60-74'][effect_type]
                    else:
                        age_modifier = 1.0
            except (TypeError, ValueError):
                pass

        final_rr = rr * disease_modifier * age_modifier

        # 应用最终RR上限
        final_rr = min(final_rr, self.rr_cap_cumulative)

        return final_rr, {
            'base_rr': rr,
            'disease_modifier': disease_modifier,
            'age_modifier': age_modifier,
            'final_rr': final_rr,
            'mmt': mmt,
            'temperature': temperature,
            'deviation_from_mmt': abs(temperature - mmt),
            'literature_weight': self.literature_weight,
            'rr_cap_applied': final_rr >= self.rr_cap_cumulative
        }
    
    def _get_base_rr(self, temperature):
        """获取基础相对风险 (整合文献先验)"""
        # 确保有MMT
        mmt = self.mmt if self.mmt is not None else 23.0

        # 从temperature_rr映射中查找最近的温度
        if not self.temperature_rr:
            # 使用文献先验
            rr = self._get_literature_rr(temperature)
            return min(rr, self.rr_cap_single)

        try:
            # 找到最近的温度对应的RR
            nearest_temp = min(self.temperature_rr.keys(), key=lambda x: abs(x - temperature))
            local_rr = float(self.temperature_rr[nearest_temp])

            # 如果温度距离最近点超过2度，使用文献值插值
            if abs(temperature - nearest_temp) > 2:
                lit_rr = self._get_literature_rr(temperature)
                # 距离越远，越依赖文献
                distance_weight = min(abs(temperature - nearest_temp) / 5.0, 1.0)
                rr = (1 - distance_weight) * local_rr + distance_weight * lit_rr
            else:
                rr = local_rr

            # 应用RR上限
            return min(rr, self.rr_cap_single)

        except (ValueError, TypeError):
            # 如果出错，使用文献先验
            rr = self._get_literature_rr(temperature)
            return min(rr, self.rr_cap_single)
    
    def _apply_lag_effects(self, current_temp, lag_temps, base_rr):
        """应用滞后效应"""
        # 确定使用热效应还是冷效应的滞后权重
        if current_temp >= self.mmt:
            weights = self.lag_weights.get('heat', {})
        else:
            weights = self.lag_weights.get('cold', {})
        
        # 计算加权RR
        total_rr = base_rr * weights.get(0, 0.2)  # 当天权重
        
        # lag_temps is expected to include lag0..lagN; we want to use lag1..max_lag
        for lag, temp in enumerate(lag_temps[: self.max_lag + 1]):
            if lag > 0:
                lag_rr = self._get_base_rr(temp)
                weight = weights.get(lag, 0.1)
                total_rr += lag_rr * weight
        
        return total_rr
    
    def calculate_attributable_fraction(self, temperature, baseline_temp=None):
        """
        计算可归因分数 (Attributable Fraction)
        AF = (RR - 1) / RR
        """
        if baseline_temp is None:
            baseline_temp = self.mmt
        
        rr_t, _ = self.calculate_rr(temperature)
        rr_b, _ = self.calculate_rr(baseline_temp) if baseline_temp is not None else (1.0, {})
        rr = (rr_t / rr_b) if (rr_b and rr_b > 0) else rr_t
        af = (rr - 1) / rr if rr > 1 else 0
        
        return {
            'af': af,
            'rr': rr,
            'interpretation': f'{af*100:.1f}% 的门诊量可归因于温度偏离最优温度'
        }
    
    def get_risk_thresholds(self):
        """获取风险阈值"""
        return {
            'heat_extreme': self.percentiles.get('p95', 35),
            'heat_warning': self.percentiles.get('p90', 32),
            'cold_warning': self.percentiles.get('p10', 5),
            'cold_extreme': self.percentiles.get('p5', 2),
            'mmt': self.mmt,
            'hot_night_threshold': self.tmin_p90 if self.tmin_p90 is not None else 22
        }
    
    def identify_extreme_weather_events(self, temperature, duration=1, is_night_temp=False):
        """
        识别极端天气事件
        
        参数:
        - temperature: 温度
        - duration: 持续天数
        - is_night_temp: 是否为夜间最低温度
        
        返回:
        - event_type: 事件类型
        - severity: 严重程度
        """
        thresholds = self.get_risk_thresholds()
        events = []
        
        # 热浪检测
        if temperature >= thresholds['heat_extreme']:
            if duration >= 3:
                events.append({
                    'type': '热浪',
                    'severity': 'extreme',
                    'description': f'连续{duration}天极端高温(>{thresholds["heat_extreme"]:.1f}°C)',
                    'rr_multiplier': 1.5 + 0.1 * (duration - 3)
                })
            else:
                events.append({
                    'type': '高温',
                    'severity': 'high',
                    'description': f'极端高温({temperature:.1f}°C)',
                    'rr_multiplier': 1.3
                })
        elif temperature >= thresholds['heat_warning']:
            events.append({
                'type': '高温预警',
                'severity': 'medium',
                'description': f'高温({temperature:.1f}°C)',
                'rr_multiplier': 1.15
            })
        
        # 寒潮检测
        if temperature <= thresholds['cold_extreme']:
            if duration >= 3:
                events.append({
                    'type': '寒潮',
                    'severity': 'extreme',
                    'description': f'连续{duration}天极端低温(<{thresholds["cold_extreme"]:.1f}°C)',
                    'rr_multiplier': 1.4 + 0.08 * (duration - 3)
                })
            else:
                events.append({
                    'type': '低温',
                    'severity': 'high',
                    'description': f'极端低温({temperature:.1f}°C)',
                    'rr_multiplier': 1.25
                })
        elif temperature <= thresholds['cold_warning']:
            events.append({
                'type': '低温预警',
                'severity': 'medium',
                'description': f'低温({temperature:.1f}°C)',
                'rr_multiplier': 1.12
            })
        
        # 热夜检测
        if is_night_temp and temperature >= thresholds.get('hot_night_threshold', 22):
            events.append({
                'type': '热夜',
                'severity': 'medium',
                'description': f'夜间最低温度过高({temperature:.1f}°C)',
                'rr_multiplier': 1.2,
                'cardiovascular_risk': 'elevated'
            })
        
        return events
    
    def get_model_summary(self):
        """获取模型摘要"""
        if not self.model_trained:
            return {'status': '模型未训练'}

        return {
            'status': '模型已训练',
            'mmt': self.mmt,
            'local_mmt': getattr(self, '_local_mmt', None),
            'literature_mmt': self.literature_priors['mmt']['typical'],
            'literature_weight': self.literature_weight,
            'percentiles': self.percentiles,
            'max_lag': self.max_lag,
            'max_lag_cold': self.max_lag_cold,
            'rr_caps': {
                'single_day': self.rr_cap_single,
                'cumulative': self.rr_cap_cumulative
            },
            'sample_counts': self.sample_counts,
            'disease_specific_models': list(self.disease_specific_rr.keys()),
            'risk_thresholds': self.get_risk_thresholds(),
            'seasonal_factors': {
                month: data['seasonal_factor']
                for month, data in self.seasonal_baseline.items()
            },
            'calibration_sources': [
                'Zeng2016_IJERPH (MMT by region)',
                'Huang2015_BMJOpen (age modifiers)',
                'Yang2012_Guangzhou (cause-specific RR)',
                'Wu2025_PMC (hot nights)',
                'Zhao2019_EnvHealth (respiratory outpatient)'
            ]
        }


# 单例实例
_dlnm_service = None

def get_dlnm_service():
    """获取DLNM服务单例"""
    global _dlnm_service
    if _dlnm_service is None:
        _dlnm_service = DLNMRiskService()
    return _dlnm_service


# 测试代码
if __name__ == '__main__':
    print("=" * 60)
    print("DLNM风险函数模型测试 (文献校准版)")
    print("=" * 60)

    # 测试不同文献权重
    for lit_weight in [0.0, 0.5, 1.0]:
        print(f"\n{'='*60}")
        print(f"文献权重 = {lit_weight}")
        print("=" * 60)

        service = DLNMRiskService(literature_weight=lit_weight)

        print("\n模型摘要:")
        summary = service.get_model_summary()
        print(f"  MMT: {summary['mmt']:.1f}°C (本地: {summary.get('local_mmt', 'N/A')}, 文献: {summary['literature_mmt']})")
        print(f"  RR上限: 单日={summary['rr_caps']['single_day']}, 累积={summary['rr_caps']['cumulative']}")

        print("\n风险计算测试 (各温度RR):")
        for temp in [0, 5, 8, 10, 15, 20, 25, 30, 35]:
            rr, breakdown = service.calculate_rr(temp)
            cap_flag = " [CAP]" if breakdown.get('rr_cap_applied') else ""
            print(f"  温度 {temp:3d}°C: RR = {rr:.3f}{cap_flag}")

        print("\n年龄分层RR (8°C, 冷效应):")
        for age in [40, 65, 80]:
            rr, breakdown = service.calculate_rr(8, age=age)
            print(f"  年龄 {age}岁: RR = {rr:.3f} (年龄修正 x{breakdown['age_modifier']:.2f})")

        print("\n病种分层RR (8°C):")
        for disease in ['respiratory', 'cardiovascular', 'digestive']:
            rr, breakdown = service.calculate_rr(8, disease_type=disease)
            name = service.disease_specific_rr[disease]['name']
            print(f"  {name}: RR = {rr:.3f} (病种修正 x{breakdown['disease_modifier']:.2f})")

    print("\n" + "=" * 60)
    print("推荐使用: literature_weight=0.5 (平衡本地数据与文献先验)")
    print("=" * 60)

