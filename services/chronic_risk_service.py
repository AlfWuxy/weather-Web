# -*- coding: utf-8 -*-
"""
模块三：慢病风险预测服务（改进版）

功能：
D1. 病种专项RR调用（呼吸系统、心脑血管等）
D2. 个体/分层放大系数
D3. 建议生成（规则库 + 可审计触发条件）

公式：
DLNMRR = min(RawDLNMRR × DLNM Disease Modifier × DLNM Age Modifier, DLNM Cap)
PersonalRisk = DLNMRR × Chronic-layer Age Amplifier × Comorbidity Amplifier

触发条件（可审计）→ 建议模板（可版本化）
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import json


from core.time_utils import utcnow
class ChronicRiskService:
    """慢病风险预测服务"""
    
    def __init__(self):
        # 年龄放大系数
        self.age_amplifiers = {
            (0, 18): {'name': '儿童青少年', 'general': 0.8, 'respiratory': 1.0, 'cardiovascular': 0.5},
            (18, 40): {'name': '青年', 'general': 0.9, 'respiratory': 0.9, 'cardiovascular': 0.7},
            (40, 60): {'name': '中年', 'general': 1.0, 'respiratory': 1.0, 'cardiovascular': 1.1},
            (60, 75): {'name': '老年', 'general': 1.3, 'respiratory': 1.4, 'cardiovascular': 1.5},
            (75, 85): {'name': '高龄', 'general': 1.5, 'respiratory': 1.6, 'cardiovascular': 1.8},
            (85, 120): {'name': '超高龄', 'general': 1.8, 'respiratory': 1.8, 'cardiovascular': 2.0}
        }
        
        # 共病放大系数
        self.comorbidity_amplifiers = {
            '高血压': {'cardiovascular': 1.4, 'general': 1.2, 'heat_sensitive': True},
            '糖尿病': {'cardiovascular': 1.3, 'general': 1.2, 'heat_sensitive': True},
            '冠心病': {'cardiovascular': 1.6, 'general': 1.3, 'cold_sensitive': True, 'heat_sensitive': True},
            'COPD': {'respiratory': 1.5, 'general': 1.3, 'cold_sensitive': True},
            '慢性阻塞性肺病': {'respiratory': 1.5, 'general': 1.3, 'cold_sensitive': True},
            '哮喘': {'respiratory': 1.4, 'general': 1.2, 'aqi_sensitive': True},
            '慢性支气管炎': {'respiratory': 1.3, 'general': 1.1, 'cold_sensitive': True},
            '心力衰竭': {'cardiovascular': 1.8, 'general': 1.5, 'heat_sensitive': True},
            '脑卒中史': {'cardiovascular': 1.5, 'general': 1.3, 'cold_sensitive': True},
            '肾病': {'cardiovascular': 1.3, 'general': 1.2},
            '关节炎': {'musculoskeletal': 1.4, 'humidity_sensitive': True, 'cold_sensitive': True}
        }
        
        # 建议规则库（可审计、可版本化）
        self.recommendation_rules = self._init_recommendation_rules()
        
        # 规则库版本
        self.rules_version = '1.0.0'
    
    def _init_recommendation_rules(self):
        """初始化建议规则库"""
        return {
            # 高温相关规则
            'heat_high_rr': {
                'name': '高温风险升高',
                'trigger': lambda ctx: ctx['rr'] >= 1.3 and ctx['temperature'] >= 32,
                'priority': 'high',
                'category': '高温风险',
                'thresholds': {'temperature': '>=32', 'rr': '>=1.3'},
                'context_fields': ['temperature', 'rr'],
                'reason_template': '气温偏高（{temperature}°C），风险升高',
                'template': '高温天气({temperature}°C)下您的{disease_type}风险显著增加(RR={rr:.2f})，建议：减少外出，保持室内凉爽，多饮水',
                'diseases': ['cardiovascular', 'general']
            },
            'heat_night': {
                'name': '热夜预警',
                'trigger': lambda ctx: ctx.get('hot_night', False),
                'priority': 'high',
                'category': '热夜预警',
                'thresholds': {'hot_night': True, 'hot_night_temp': '>22'},
                'context_fields': ['hot_night_temp'],
                'reason_template': '夜间温度偏高（{hot_night_temp}°C）',
                'template': '今晚预计为热夜(夜间温度>{hot_night_temp}°C)，心血管疾病风险增加，建议：使用空调或风扇，保持卧室凉爽',
                'diseases': ['cardiovascular']
            },
            'heat_wave': {
                'name': '热浪预警',
                'trigger': lambda ctx: ctx.get('heat_wave_days', 0) >= 3,
                'priority': 'urgent',
                'category': '热浪预警',
                'thresholds': {'heat_wave_days': '>=3'},
                'context_fields': ['heat_wave_days'],
                'reason_template': '连续高温{heat_wave_days}天，累积风险增加',
                'template': '连续{heat_wave_days}天高温热浪，累积风险显著增加，建议：尽量待在室内，避免剧烈活动，如有不适立即就医',
                'diseases': ['cardiovascular', 'general']
            },
            
            # 低温相关规则
            'cold_high_rr': {
                'name': '低温风险升高',
                'trigger': lambda ctx: ctx['rr'] >= 1.2 and ctx['temperature'] <= 5,
                'priority': 'high',
                'category': '低温风险',
                'thresholds': {'temperature': '<=5', 'rr': '>=1.2'},
                'context_fields': ['temperature', 'rr'],
                'reason_template': '气温偏低（{temperature}°C），风险上升',
                'template': '低温天气({temperature}°C)下您的{disease_type}风险增加(RR={rr:.2f})，建议：注意保暖，避免受凉',
                'diseases': ['respiratory', 'cardiovascular']
            },
            'cold_wave': {
                'name': '寒潮预警',
                'trigger': lambda ctx: ctx.get('cold_wave_days', 0) >= 3,
                'priority': 'urgent',
                'category': '寒潮预警',
                'thresholds': {'cold_wave_days': '>=3'},
                'context_fields': ['cold_wave_days'],
                'reason_template': '连续低温{cold_wave_days}天，风险持续',
                'template': '连续{cold_wave_days}天低温寒潮，呼吸道疾病风险持续升高，建议：室内保暖，减少外出，预防感冒',
                'diseases': ['respiratory']
            },
            
            # 空气质量相关
            'aqi_high': {
                'name': '空气质量较差',
                'trigger': lambda ctx: ctx.get('aqi', 0) >= 150,
                'priority': 'high',
                'category': '空气质量',
                'thresholds': {'aqi': '>=150'},
                'context_fields': ['aqi'],
                'reason_template': '空气质量较差（AQI {aqi}）',
                'template': '空气质量差(AQI={aqi})，呼吸系统疾病风险增加，建议：减少户外活动，外出佩戴口罩，关闭门窗',
                'diseases': ['respiratory']
            },
            'aqi_moderate': {
                'name': '空气质量一般',
                'trigger': lambda ctx: 100 <= ctx.get('aqi', 0) < 150,
                'priority': 'medium',
                'category': '空气质量',
                'thresholds': {'aqi': '100-149'},
                'context_fields': ['aqi'],
                'reason_template': '空气质量一般（AQI {aqi}）',
                'template': '空气质量一般(AQI={aqi})，敏感人群建议减少户外运动',
                'diseases': ['respiratory']
            },
            
            # 慢病管理
            'elderly_extreme_weather': {
                'name': '老年极端天气',
                'trigger': lambda ctx: ctx['age'] >= 65 and (ctx['temperature'] <= 5 or ctx['temperature'] >= 32),
                'priority': 'high',
                'category': '老年健康',
                'thresholds': {'age': '>=65', 'temperature': '<=5 or >=32'},
                'context_fields': ['age', 'temperature'],
                'reason_template': '年龄较高且遇到极端天气',
                'template': '您属于{age}岁老年人，在当前极端天气下需特别注意：定期测量血压，按时服药，如有不适及时就医',
                'diseases': ['general']
            },
            'comorbidity_risk': {
                'name': '多病共存',
                'trigger': lambda ctx: len(ctx.get('chronic_diseases', [])) >= 2 and ctx['rr'] >= 1.2,
                'priority': 'high',
                'category': '多病共存',
                'thresholds': {'disease_count': '>=2', 'rr': '>=1.2'},
                'context_fields': ['disease_count', 'rr'],
                'reason_template': '多种慢病叠加，风险提高',
                'template': '您有{disease_count}种慢性病共存，当前天气条件下综合风险较高，建议：密切关注身体状况，保持规律用药',
                'diseases': ['general']
            },
            
            # 服药提醒
            'medication_reminder': {
                'name': '规律用药提醒',
                'trigger': lambda ctx: ctx.get('has_chronic_disease', False),
                'priority': 'low',
                'category': '用药提醒',
                'thresholds': {'has_chronic_disease': True},
                'context_fields': ['disease_count'],
                'reason_template': '慢病管理需要规律用药',
                'template': '请按时服用您的慢性病药物，不要自行停药或改变剂量',
                'diseases': ['general']
            }
        }
    
    def get_age_amplifier(self, age, disease_type='general'):
        """获取年龄放大系数"""
        for (age_min, age_max), amplifiers in self.age_amplifiers.items():
            if age_min <= age < age_max:
                return amplifiers.get(disease_type, amplifiers['general'])
        return 1.0
    
    def get_comorbidity_amplifier(self, chronic_diseases, disease_type='general'):
        """
        获取共病放大系数
        
        多个共病时取最大值，并有叠加效应
        """
        if not chronic_diseases:
            return 1.0
        
        if isinstance(chronic_diseases, str):
            chronic_diseases = [chronic_diseases]
        
        max_amplifier = 1.0
        additional_factor = 0
        
        for disease in chronic_diseases:
            for key, amplifiers in self.comorbidity_amplifiers.items():
                if key in disease or disease in key:
                    amp = amplifiers.get(disease_type, amplifiers.get('general', 1.0))
                    if amp > max_amplifier:
                        additional_factor += (max_amplifier - 1) * 0.3 if max_amplifier > 1 else 0
                        max_amplifier = amp
                    else:
                        additional_factor += (amp - 1) * 0.3
        
        # 多病叠加效应
        return max_amplifier + additional_factor

    def _parse_vital_number(self, value, lower, upper):
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if parsed < lower or parsed > upper:
            return None
        return parsed

    def _analyze_submitted_vitals(self, user_info):
        """保守评估用户本次提交的血压/血糖指标。"""
        vitals = user_info.get('vitals') if isinstance(user_info.get('vitals'), dict) else {}
        sbp = self._parse_vital_number(user_info.get('sbp', vitals.get('sbp')), 60, 260)
        fbg = self._parse_vital_number(user_info.get('fbg', vitals.get('fbg')), 2.0, 30.0)

        score_adjustment = 0.0
        factors = []
        recommendations = []

        if sbp is not None:
            if sbp >= 180:
                score_adjustment += 14
                factors.append(f'近7天最高收缩压{sbp:g}mmHg，已按明显偏高处理')
                recommendations.append('近7天最高血压明显偏高，建议尽快联系社区医生复核。')
            elif sbp >= 160:
                score_adjustment += 10
                factors.append(f'近7天最高收缩压{sbp:g}mmHg，血压控制偏高')
                recommendations.append('建议连续记录早晚血压，并按医嘱调整复诊计划。')
            elif sbp >= 140:
                score_adjustment += 6
                factors.append(f'近7天最高收缩压{sbp:g}mmHg，血压略高')

        if fbg is not None:
            if fbg >= 11.1:
                score_adjustment += 12
                factors.append(f'近7天空腹血糖{fbg:g}mmol/L，已按明显偏高处理')
                recommendations.append('空腹血糖明显偏高，建议尽快复测并咨询医生。')
            elif fbg >= 7.0:
                score_adjustment += 8
                factors.append(f'近7天空腹血糖{fbg:g}mmol/L，血糖控制偏高')
                recommendations.append('建议记录空腹血糖变化，减少高糖饮食并按医嘱复诊。')
            elif fbg >= 6.1:
                score_adjustment += 4
                factors.append(f'近7天空腹血糖{fbg:g}mmol/L，血糖略高')

        return {
            'sbp': sbp,
            'fbg': fbg,
            'score_adjustment': min(score_adjustment, 18.0),
            'factors': factors,
            'recommendations': recommendations,
        }

    def _get_score_risk_level(self, score):
        if score >= 70:
            return '高风险'
        if score >= 40:
            return '中风险'
        return '低风险'
    
    def predict_individual_risk(self, user_info, weather_data, target_diseases=None):
        """
        预测个体慢病风险
        
        参数:
        - user_info: 用户信息 {age, gender, chronic_diseases, ...}
        - weather_data: 天气数据 {temperature, humidity, aqi, ...}
        - target_diseases: 目标疾病类型列表
        
        返回:
        - risks: 各病种风险
        - recommendations: 个性化建议
        """
        from services.dlnm_risk_service import get_dlnm_service
        
        dlnm = get_dlnm_service()
        
        # 安全获取和转换年龄
        try:
            age = int(user_info.get('age', 50))
            if age < 0 or age > 150:
                age = 50
        except (TypeError, ValueError):
            age = 50
        
        # 安全处理慢性病列表
        chronic_diseases = user_info.get('chronic_diseases', [])
        if isinstance(chronic_diseases, str):
            if chronic_diseases:
                try:
                    chronic_diseases = json.loads(chronic_diseases)
                except json.JSONDecodeError:
                    chronic_diseases = [chronic_diseases]
            else:
                chronic_diseases = []
        elif chronic_diseases is None:
            chronic_diseases = []
        
        # 确保是列表
        if not isinstance(chronic_diseases, list):
            chronic_diseases = [str(chronic_diseases)] if chronic_diseases else []
        
        # 安全获取温度
        try:
            temperature = float(weather_data.get('temperature', 20))
        except (TypeError, ValueError):
            temperature = 20.0
        
        # 确定目标疾病类型
        if target_diseases is None:
            target_diseases = self._infer_disease_types(chronic_diseases)
        
        risks = {}
        max_risk = {'rr': 1.0, 'disease_type': 'general'}
        vital_adjustment = self._analyze_submitted_vitals(user_info)
        
        for disease_type in target_diseases:
            # 获取病种专项RR
            dlnm_adjusted_rr, dlnm_breakdown = dlnm.calculate_rr(
                temperature, 
                disease_type=disease_type,
                age=age
            )

            # DLNM 内部已经包含病种与年龄修正，这里拆开保存，避免和慢病层修正混在一起。
            raw_dlnm_rr = float(
                dlnm_breakdown.get('raw_dlnm_rr', dlnm_breakdown.get('base_rr', dlnm_adjusted_rr))
            )
            dlnm_disease_modifier = float(
                dlnm_breakdown.get('dlnm_disease_modifier', dlnm_breakdown.get('disease_modifier', 1.0))
            )
            dlnm_age_modifier = float(
                dlnm_breakdown.get('dlnm_age_modifier', dlnm_breakdown.get('age_modifier', 1.0))
            )
            dlnm_adjusted_rr = float(dlnm_adjusted_rr)
            
            # 年龄放大
            age_amp = self.get_age_amplifier(age, disease_type)
            
            # 共病放大
            comorbidity_amp = self.get_comorbidity_amplifier(chronic_diseases, disease_type)
            
            # 最终风险
            personal_rr = dlnm_adjusted_rr * age_amp * comorbidity_amp
            risk_score_before_vitals = min(100, round(personal_rr * 30, 1))
            
            risks[disease_type] = {
                # base_rr 保留旧接口含义：DLNM 内层修正并限幅后的 RR。
                'base_rr': round(dlnm_adjusted_rr, 3),
                'raw_dlnm_rr': round(raw_dlnm_rr, 4),
                'dlnm_disease_modifier': round(dlnm_disease_modifier, 4),
                'dlnm_age_modifier': round(dlnm_age_modifier, 4),
                'dlnm_uncapped_rr': round(
                    float(dlnm_breakdown.get(
                        'uncapped_final_rr',
                        raw_dlnm_rr * dlnm_disease_modifier * dlnm_age_modifier
                    )),
                    4
                ),
                'dlnm_adjusted_rr': round(dlnm_adjusted_rr, 4),
                'dlnm_rr_cap': dlnm_breakdown.get('rr_cap'),
                'dlnm_rr_cap_applied': bool(dlnm_breakdown.get('rr_cap_applied')),
                'dlnm_calculation_branch': dlnm_breakdown.get('calculation_branch', 'legacy'),
                'age_amplifier': round(age_amp, 2),
                'chronic_age_amplifier': round(age_amp, 2),
                'comorbidity_amplifier': round(comorbidity_amp, 2),
                'personal_rr': round(personal_rr, 3),
                'risk_level': self._get_risk_level(personal_rr),
                'risk_score_before_vitals': risk_score_before_vitals,
                'risk_score': risk_score_before_vitals,
            }

            if vital_adjustment['score_adjustment']:
                relevance = 1.0 if disease_type in ('cardiovascular', 'general') else 0.4
                adjusted_score = min(
                    100,
                    risks[disease_type]['risk_score'] + vital_adjustment['score_adjustment'] * relevance
                )
                risks[disease_type]['risk_score'] = round(adjusted_score, 1)
                risks[disease_type]['vital_adjustment'] = round(vital_adjustment['score_adjustment'] * relevance, 1)
                risks[disease_type]['vital_relevance'] = relevance
                risks[disease_type]['risk_level'] = self._get_score_risk_level(adjusted_score)
            
            if personal_rr > max_risk['rr']:
                max_risk = {'rr': personal_rr, 'disease_type': disease_type}
        
        # 生成个性化建议
        context = {
            'age': age,
            'temperature': temperature,
            'rr': max_risk['rr'],
            'disease_type': max_risk['disease_type'],
            'chronic_diseases': chronic_diseases,
            'has_chronic_disease': len(chronic_diseases) > 0,
            'disease_count': len(chronic_diseases),
            'aqi': weather_data.get('aqi', 50),
            'hot_night': weather_data.get('tmin', 15) >= 22 if 'tmin' in weather_data else False,
            'hot_night_temp': weather_data.get('tmin', 22),
            'heat_wave_days': weather_data.get('heat_wave_days', 0),
            'cold_wave_days': weather_data.get('cold_wave_days', 0)
        }
        
        recommendations = self._generate_recommendations(context, risks)
        if vital_adjustment['recommendations']:
            existing_advice = {
                item.get('advice')
                for item in recommendations
                if isinstance(item, dict)
            }
            for advice in vital_adjustment['recommendations']:
                if advice in existing_advice:
                    continue
                recommendations.append({
                    'rule_id': 'submitted_vitals',
                    'category': '自测指标',
                    'priority': 'medium',
                    'advice': advice,
                    'applicable_diseases': ['cardiovascular', 'general']
                })
                existing_advice.add(advice)
        explain, triggered_rules = self.build_explain(context, recommendations)
        
        # 确定总体风险等级
        overall_rr = max(r['personal_rr'] for r in risks.values()) if risks else 1.0
        overall_score = max((r.get('risk_score', 0) for r in risks.values()), default=round(overall_rr * 30, 1))
        overall_level = self._get_score_risk_level(overall_score)
        
        return {
            'user_profile': {
                'age': age,
                'age_group': self._get_age_group_name(age),
                'chronic_diseases': chronic_diseases,
                'disease_count': len(chronic_diseases),
                'vitals': {
                    'sbp': vital_adjustment['sbp'],
                    'fbg': vital_adjustment['fbg']
                }
            },
            'weather': {
                'temperature': temperature,
                'aqi': weather_data.get('aqi'),
                'humidity': weather_data.get('humidity')
            },
            'disease_risks': risks,
            'overall_risk': {
                'rr': round(overall_rr, 3),
                'level': overall_level,
                'color': 'danger' if overall_level == '高风险' else 'warning' if overall_level == '中风险' else 'success',
                'score': min(100, round(overall_score, 1))
            },
            'recommendations': recommendations,
            'vital_adjustment': vital_adjustment,
            'explain': explain,
            'rule_version': self.rules_version,
            'triggered_rules': triggered_rules,
            'alert': overall_level == '高风险'
        }
    
    def _infer_disease_types(self, chronic_diseases):
        """根据慢性病推断相关疾病类型"""
        types = {'general'}  # 总是包含通用类型
        
        for disease in chronic_diseases:
            disease_lower = disease.lower() if isinstance(disease, str) else ''
            
            if any(kw in disease_lower for kw in ['心', '血压', '冠心', '心力', '心脏']):
                types.add('cardiovascular')
            
            if any(kw in disease_lower for kw in ['呼吸', '肺', '支气管', '哮喘']):
                types.add('respiratory')
            
            if any(kw in disease_lower for kw in ['消化', '胃', '肠']):
                types.add('digestive')
        
        return list(types)
    
    def _get_risk_level(self, rr):
        """根据RR确定风险等级"""
        if rr >= 1.5:
            return '高风险'
        elif rr >= 1.2:
            return '中风险'
        else:
            return '低风险'
    
    def _get_age_group_name(self, age):
        """获取年龄段名称"""
        for (age_min, age_max), info in self.age_amplifiers.items():
            if age_min <= age < age_max:
                return info['name']
        return '未知'

    def _build_safe_context(self, context):
        """构建安全上下文"""
        return {
            'age': context.get('age', 50),
            'temperature': context.get('temperature', 20),
            'rr': context.get('rr', 1.0),
            'disease_type': context.get('disease_type', 'general'),
            'chronic_diseases': context.get('chronic_diseases', []),
            'has_chronic_disease': context.get('has_chronic_disease', False),
            'disease_count': context.get('disease_count', 0),
            'aqi': context.get('aqi', 50),
            'hot_night': context.get('hot_night', False),
            'hot_night_temp': context.get('hot_night_temp', 22),
            'heat_wave_days': context.get('heat_wave_days', 0),
            'cold_wave_days': context.get('cold_wave_days', 0)
        }

    def _evaluate_triggered_rules(self, context):
        """评估触发规则"""
        triggered_rules = []
        safe_context = self._build_safe_context(context)

        for rule_id, rule in self.recommendation_rules.items():
            try:
                trigger_func = rule.get('trigger')
                if callable(trigger_func) and trigger_func(safe_context):
                    triggered_rules.append((rule_id, rule))
            except Exception:
                continue

        return triggered_rules, safe_context
    
    def _generate_recommendations(self, context, risks):
        """生成个性化建议"""
        recommendations = []
        triggered_rules, safe_context = self._evaluate_triggered_rules(context)
        
        # 按优先级排序
        priority_order = {'urgent': 0, 'high': 1, 'medium': 2, 'low': 3}
        triggered_rules.sort(key=lambda x: priority_order.get(x[1].get('priority', 'low'), 99))
        
        # 生成建议
        seen_categories = set()
        for rule_id, rule in triggered_rules:
            category = rule.get('category', '健康建议')
            if category in seen_categories:
                continue  # 每个类别只保留一条
            
            seen_categories.add(category)
            
            # 格式化建议文本
            try:
                advice_text = rule.get('template', '').format(**safe_context)
            except (KeyError, ValueError):
                advice_text = rule.get('template', '请注意健康')
            
            recommendations.append({
                'rule_id': rule_id,
                'category': category,
                'priority': rule.get('priority', 'low'),
                'advice': advice_text,
                'applicable_diseases': rule.get('diseases', ['general'])
            })
        
        # 至少有一条建议
        if not recommendations:
            recommendations.append({
                'rule_id': 'default',
                'category': '日常健康',
                'priority': 'low',
                'advice': '保持健康生活方式，适量运动，均衡饮食，如有不适及时就医',
                'applicable_diseases': ['general']
            })
        
        return recommendations

    def build_explain(self, context, actions_source=None):
        """生成可解释输出"""
        triggered_rules, safe_context = self._evaluate_triggered_rules(context)
        triggered_rules.sort(key=lambda x: {'urgent': 0, 'high': 1, 'medium': 2, 'low': 3}.get(x[1].get('priority', 'low'), 99))

        triggered_output = []
        reasons = []
        now_str = utcnow().isoformat()
        for rule_id, rule in triggered_rules:
            reason_template = rule.get('reason_template')
            if reason_template:
                try:
                    reason_text = reason_template.format(**safe_context)
                except Exception:
                    reason_text = reason_template
                if reason_text not in reasons and len(reasons) < 3:
                    reasons.append(reason_text)
            params = {}
            for key in rule.get('context_fields', []):
                params[key] = safe_context.get(key)
            triggered_output.append({
                'rule_id': rule_id,
                'name': rule.get('name', rule.get('category', rule_id)),
                'thresholds': rule.get('thresholds', {}),
                'params': params,
                'triggered_at': now_str
            })

        # 行为建议
        actions = []
        if actions_source:
            for item in actions_source:
                advice = item.get('advice') if isinstance(item, dict) else None
                if advice and advice not in actions:
                    actions.append(advice)
                if len(actions) >= 5:
                    break
        if not actions:
            actions = [
                '注意补水，避免在高温或低温时段外出。',
                '按时服药，规律作息，保持室内通风。',
                '如有不适，请及时休息并观察。'
            ]

        # 紧急分流提示
        escalation = []
        if safe_context.get('rr', 1.0) >= 1.5 or safe_context.get('heat_wave_days', 0) >= 3 or safe_context.get('cold_wave_days', 0) >= 3:
            escalation.append('如出现胸痛、呼吸困难、意识模糊等，请立即就医或拨打120。')
        if safe_context.get('age', 0) >= 75 or safe_context.get('disease_count', 0) >= 2:
            escalation.append('建议及时联系家属或村医协助观察。')
        if safe_context.get('aqi', 0) >= 200:
            escalation.append('若持续咳喘或胸闷，请联系医生评估。')

        return {
            'reasons': reasons[:3],
            'actions': actions[:5],
            'escalation': escalation[:3],
            'disclaimer': '风险提示不是诊断，如有不适请及时就医。'
        }, triggered_output
    
    def predict_population_risk(self, population_info, weather_data):
        """
        预测人群风险（用于社区/医生端）
        
        参数:
        - population_info: 人群信息 {age_distribution, chronic_disease_prevalence, ...}
        - weather_data: 天气数据
        
        返回:
        - stratified_risks: 分层风险
        - high_risk_groups: 高危人群识别
        """
        from services.dlnm_risk_service import get_dlnm_service
        
        dlnm = get_dlnm_service()
        temperature = weather_data.get('temperature', 20)
        
        # 定义人群分层
        strata = {
            'elderly_respiratory': {
                'description': '老年呼吸系统疾病患者',
                'age_range': (65, 120),
                'disease_type': 'respiratory',
                'chronic_diseases': ['COPD', '慢性支气管炎']
            },
            'elderly_cardiovascular': {
                'description': '老年心血管疾病患者',
                'age_range': (65, 120),
                'disease_type': 'cardiovascular',
                'chronic_diseases': ['高血压', '冠心病']
            },
            'middle_aged_chronic': {
                'description': '中年慢病患者',
                'age_range': (45, 65),
                'disease_type': 'general',
                'chronic_diseases': ['高血压', '糖尿病']
            },
            'general_elderly': {
                'description': '一般老年人群',
                'age_range': (60, 120),
                'disease_type': 'general',
                'chronic_diseases': []
            },
            'general_population': {
                'description': '一般人群',
                'age_range': (18, 60),
                'disease_type': 'general',
                'chronic_diseases': []
            }
        }
        
        stratified_risks = {}
        high_risk_groups = []
        
        for stratum_id, stratum in strata.items():
            # 代表性年龄
            rep_age = (stratum['age_range'][0] + stratum['age_range'][1]) // 2
            
            user_info = {
                'age': rep_age,
                'chronic_diseases': stratum['chronic_diseases']
            }
            
            result = self.predict_individual_risk(
                user_info, 
                weather_data, 
                [stratum['disease_type']]
            )
            
            overall_risk = result['overall_risk']
            
            stratified_risks[stratum_id] = {
                'description': stratum['description'],
                'rr': overall_risk['rr'],
                'level': overall_risk['level'],
                'score': overall_risk['score']
            }
            
            if overall_risk['level'] == '高风险':
                high_risk_groups.append({
                    'group': stratum['description'],
                    'rr': overall_risk['rr'],
                    'recommendation': result['recommendations'][0]['advice'] if result['recommendations'] else '加强健康监测'
                })
        
        # 按风险排序
        sorted_strata = sorted(
            stratified_risks.items(), 
            key=lambda x: x[1]['rr'], 
            reverse=True
        )
        
        return {
            'stratified_risks': dict(sorted_strata),
            'high_risk_groups': high_risk_groups,
            'weather': {
                'temperature': temperature,
                'aqi': weather_data.get('aqi')
            },
            'overall_summary': {
                'highest_risk_group': sorted_strata[0][1]['description'] if sorted_strata else None,
                'highest_rr': sorted_strata[0][1]['rr'] if sorted_strata else 1.0,
                'high_risk_count': len(high_risk_groups)
            }
        }
    
    def get_rules_version(self):
        """获取规则库版本"""
        return {
            'version': self.rules_version,
            'total_rules': len(self.recommendation_rules),
            'categories': list(set(r['category'] for r in self.recommendation_rules.values()))
        }


# 单例实例
_chronic_service = None

def get_chronic_service():
    """获取慢病风险服务单例"""
    global _chronic_service
    if _chronic_service is None:
        _chronic_service = ChronicRiskService()
    return _chronic_service


# 测试代码
if __name__ == '__main__':
    print("=" * 60)
    print("慢病风险预测服务测试")
    print("=" * 60)
    
    service = ChronicRiskService()
    
    print("\n规则库版本:")
    print(json.dumps(service.get_rules_version(), ensure_ascii=False, indent=2))
    
    print("\n个体风险预测测试:")
    
    # 测试用例1：老年高血压患者 + 高温天气
    user1 = {'age': 72, 'chronic_diseases': ['高血压', '冠心病']}
    weather1 = {'temperature': 35, 'humidity': 85, 'aqi': 80}
    
    result1 = service.predict_individual_risk(user1, weather1)
    print("\n用例1：72岁高血压冠心病患者 + 35°C高温")
    print(f"  总体风险: {result1['overall_risk']['level']} (RR={result1['overall_risk']['rr']})")
    print("  建议:")
    for rec in result1['recommendations']:
        print(f"    [{rec['priority']}] {rec['advice']}")
    
    # 测试用例2：老年COPD患者 + 低温天气
    user2 = {'age': 68, 'chronic_diseases': ['COPD', '慢性支气管炎']}
    weather2 = {'temperature': 2, 'humidity': 60, 'aqi': 120}
    
    result2 = service.predict_individual_risk(user2, weather2)
    print("\n用例2：68岁COPD患者 + 2°C低温 + AQI 120")
    print(f"  总体风险: {result2['overall_risk']['level']} (RR={result2['overall_risk']['rr']})")
    print("  建议:")
    for rec in result2['recommendations']:
        print(f"    [{rec['priority']}] {rec['advice']}")
    
    print("\n人群分层风险预测:")
    pop_result = service.predict_population_risk({}, weather1)
    print(f"  最高风险人群: {pop_result['overall_summary']['highest_risk_group']}")
    print(f"  最高RR: {pop_result['overall_summary']['highest_rr']}")
    print("  高危人群:")
    for group in pop_result['high_risk_groups']:
        print(f"    - {group['group']}: RR={group['rr']:.2f}")
