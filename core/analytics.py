# -*- coding: utf-8 -*-
"""Analytics helpers."""
import math
from datetime import timedelta

from core.db_models import HealthRiskAssessment


def pearson_corr(x_values, y_values):
    """计算皮尔逊相关系数"""
    if not x_values or not y_values or len(x_values) != len(y_values) or len(x_values) < 2:
        return None
    x_mean = sum(x_values) / len(x_values)
    y_mean = sum(y_values) / len(y_values)
    num = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_values, y_values))
    den_x = math.sqrt(sum((x - x_mean) ** 2 for x in x_values))
    den_y = math.sqrt(sum((y - y_mean) ** 2 for y in y_values))
    if den_x == 0 or den_y == 0:
        return None
    return num / (den_x * den_y)


def get_high_risk_streak(user_id, max_days=14):
    """计算连续高风险天数"""
    if not user_id:
        return 0
    assessments = HealthRiskAssessment.query.filter_by(
        user_id=user_id
    ).order_by(HealthRiskAssessment.assessment_date.desc()).limit(max_days * 2).all()
    if not assessments:
        return 0
    streak = 0
    latest_date = assessments[0].assessment_date.date()
    expected_date = latest_date
    seen = set()
    for assessment in assessments:
        day = assessment.assessment_date.date()
        if day in seen:
            continue
        seen.add(day)
        if day != expected_date:
            break
        if assessment.risk_level != '高风险':
            break
        streak += 1
        expected_date = expected_date - timedelta(days=1)
    return streak
