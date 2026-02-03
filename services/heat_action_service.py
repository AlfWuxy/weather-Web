# -*- coding: utf-8 -*-
"""Heat action helpers (risk scoring only, no UI text)."""
from utils.parsers import parse_float


class HeatActionService:
    """Compute heat risk level from heat index, night min, and hot-day streak."""

    def calculate_heat_risk(self, weather_data, consecutive_hot_days=None):
        temp = parse_float(weather_data.get('temperature'))
        humidity = parse_float(weather_data.get('humidity'))
        heat_index = self._heat_index_c(temp, humidity)

        night_min = parse_float(weather_data.get('temperature_min'))
        if night_min is None:
            night_min = temp

        streak_days = consecutive_hot_days
        if streak_days is None:
            streak_days = 0
        try:
            streak_days = int(streak_days)
        except (TypeError, ValueError):
            streak_days = 0
        if streak_days < 0:
            streak_days = 0

        hi_score = self._normalize_heat_index(heat_index)
        night_score = self._normalize_night_temp(night_min)
        streak_score = self._normalize_hot_days(streak_days)

        factors = [
            {
                'key': 'heat_index',
                'label': '体感热',
                'value': self._format_temp_value(heat_index),
                'score': hi_score,
                'weight': 0.5
            },
            {
                'key': 'night_min',
                'label': '夜间最低温',
                'value': self._format_temp_value(night_min),
                'score': night_score,
                'weight': 0.3
            },
            {
                'key': 'hot_streak',
                'label': '连续高温天数',
                'value': f'{streak_days}天',
                'score': streak_score,
                'weight': 0.2
            }
        ]

        weighted_sum = sum(item['score'] * item['weight'] for item in factors)
        risk_score = round(max(0, min(weighted_sum * 100, 100)), 1)
        risk_level = self._risk_level_from_score(risk_score)

        return {
            'risk_level': risk_level,
            'risk_score': risk_score,
            'risk_score_norm': round(risk_score / 100.0, 3),
            'heat_index': heat_index,
            'night_min': night_min,
            'consecutive_hot_days': streak_days,
            'factor_scores': factors
        }

    def build_risk_reasons(self, heat_result):
        """Build normalized contributing factors for UI display."""
        factors = heat_result.get('factor_scores', []) if heat_result else []
        if not factors:
            return []

        weighted_values = [item['score'] * item['weight'] for item in factors]
        total = sum(weighted_values) or 1
        weights = [max(0, round(value / total * 100)) for value in weighted_values]
        diff = 100 - sum(weights)
        if weights:
            max_idx = max(range(len(weights)), key=lambda i: weights[i])
            weights[max_idx] = max(0, weights[max_idx] + diff)

        normalized = []
        for item, weight in zip(factors, weights):
            normalized.append({
                'label': item['label'],
                'value': item['value'],
                'weight': weight
            })
        return normalized

    def _heat_index_c(self, temp, humidity):
        if temp is None:
            return None
        if humidity is None:
            return temp
        t = float(temp)
        r = float(humidity)
        hi = (
            -8.784695
            + 1.61139411 * t
            + 2.338549 * r
            - 0.14611605 * t * r
            - 0.012308094 * t * t
            - 0.016424828 * r * r
            + 0.002211732 * t * t * r
            + 0.00072546 * t * r * r
            - 0.000003582 * t * t * r * r
        )
        return round(hi, 1)

    def _normalize_heat_index(self, heat_index):
        if heat_index is None:
            return 0
        return self._clamp((heat_index - 30) / 12, 0, 1)

    def _normalize_night_temp(self, night_min):
        if night_min is None:
            return 0
        return self._clamp((night_min - 24) / 6, 0, 1)

    def _normalize_hot_days(self, days):
        if days is None:
            return 0
        return self._clamp((days - 1) / 4, 0, 1)

    def _risk_level_from_score(self, score):
        if score >= 75:
            return 'extreme'
        if score >= 55:
            return 'high'
        if score >= 35:
            return 'medium'
        return 'low'

    @staticmethod
    def _format_temp_value(value):
        if value is None:
            return '--'
        return f'{value:.1f}°C'

    @staticmethod
    def _clamp(value, min_value, max_value):
        return max(min_value, min(value, max_value))
