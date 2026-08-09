# -*- coding: utf-8 -*-
"""未校准个人概率与 Likelihood 矩阵停用回归测试。"""


def test_personal_probability_and_likelihood_matrix_are_disabled(monkeypatch):
    from services.health_risk_service import HealthRiskService

    ages = []

    class FakeDLNM:
        def calculate_rr(self, _temperature, lag_temperatures=None, disease_type=None, age=None):
            del lag_temperatures, disease_type
            ages.append(age)
            return 1.2, {'dlnm_age_modifier': 1.0}

    class FakeChronicService:
        def predict_individual_risk(self, _user, _weather):
            return {
                'overall_risk': {'score': 45.0},
                'disease_risks': {},
                'recommendations': [],
                'explain': {'reasons': [], 'actions': [], 'escalation': []},
                'triggered_rules': [],
                'rule_version': 'test',
            }

    class FakeWeatherService:
        def identify_extreme_weather(self, _weather):
            return {'conditions': []}

    monkeypatch.setattr('services.dlnm_risk_service.get_dlnm_service', lambda: FakeDLNM())
    monkeypatch.setattr('services.chronic_risk_service.get_chronic_service', lambda: FakeChronicService())
    monkeypatch.setattr('services.weather_service.WeatherService', FakeWeatherService)

    service = HealthRiskService()
    monkeypatch.setattr(
        service,
        '_build_community_context',
        lambda _community: {
            'community': '测试社区',
            'vulnerability_index': 45.0,
            'burden_score': 30.0,
        },
    )

    result = service.assess_personal_weather_health_risk(
        {
            'age': 75,
            'gender': '女',
            'community': '测试社区',
            'chronic_diseases': ['高血压'],
        },
        {'temperature': 32, 'humidity': 60, 'aqi': None},
    )

    assert ages == [None]
    assert result['weather']['aqi'] is None
    assert result['weather']['aqi_available'] is False
    assert result['probability_status'] == 'disabled_uncalibrated'
    assert result['risk_probabilities'] == {'low': None, 'medium': None, 'high': None}
    assert result['high_risk_probability'] is None
    assert result['cap_semantics']['certainty'] == 'unknown'
    assert result['impact_likelihood']['available'] is False
    assert result['impact_likelihood']['likelihood_score'] is None
    assert result['impact_likelihood']['matrix_score'] is None
