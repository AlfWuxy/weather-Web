"""合成边界：人口规模不能冒充个人危险度，口径未知不得静默补齐。"""
import json

import pytest

from services.community_risk_service import CommunityRiskService
from scripts.community_risk_sensitivity import analyze, synthetic_payload


def profile(population=100, days=1):
    return dict(population=population, baseline_visits=population * 0.01 * days,
                baseline_period_days=days, baseline_population_scope='all_residents',
                elderly_ratio=0.3, chronic_disease_ratio=0.2, green_space_ratio=0.2,
                heat_island_index=0.3, medical_accessibility=0.7, uses_proxy_values=False)


@pytest.mark.parametrize('rr', [0.8, 1, 1.5, 3])
def test_equal_rates_ignore_population_size_in_all_risk_consumers(monkeypatch, rr):
    service = CommunityRiskService()
    service.community_profiles = {'small': profile(100), 'large': profile(1000)}
    monkeypatch.setattr('services.dlnm_risk_service.get_dlnm_service',
                        lambda: type('Model', (), {'calculate_rr': lambda *a, **k: (rr, {})})())
    result = service.generate_community_risk_map({'temperature': 35})
    small, large = sorted(result['rankings'], key=lambda row: row['population'])
    for field in ('risk_score', 'weather_hazard_score', 'risk_index', 'risk_level',
                  'relative_index', 'percentile_rank', 'rank', 'impact_bucket',
                  'matrix_score', 'confidence', 'verification_priority'):
        assert small[field] == large[field], field
    assert small['excess_rate_per_1000_residents_day'] == pytest.approx(max(rr - 1, 0) * 10)
    assert large['expected_excess_visits'] == pytest.approx(10 * small['expected_excess_visits'])
    assert small['uncertainty_penalty'] == large['uncertainty_penalty'] == 1
    assert small['rate_unit'] == '人次/千居民/日'
    assert small['count_unit'] == '人次/日'


def test_period_totals_are_converted_to_same_daily_rate_and_count():
    service = CommunityRiskService()
    service.community_profiles = {'one_day': profile(100, 1), 'month': profile(100, 30)}
    one = service.calculate_community_risk_score('one_day', 1.5)
    month = service.calculate_community_risk_score('month', 1.5)
    for field in ('risk_score', 'normalized_score', 'excess_rate_per_1000_residents_day', 'expected_excess_visits'):
        assert one[field] == month[field]
    assert month['hazard_formula']['baseline_rate_per_1000_residents_day'] == 10
    assert month['expected_excess_visits'] == 0.5


@pytest.mark.parametrize('field,value', [('population', None), ('population', 0), ('population', -1),
    ('population', float('nan')), ('baseline_period_days', None), ('baseline_period_days', 0),
    ('baseline_population_scope', None), ('baseline_population_scope', 'age60plus')])
def test_unknown_or_incompatible_denominator_cannot_rank(field, value):
    service = CommunityRiskService()
    service.community_profiles = {'a': {**profile(), field: value}}
    row = service.calculate_community_risk_score('a', 2)
    assert row['ranking_eligible'] is False
    assert row['risk_score'] is row['normalized_score'] is row['expected_excess_visits'] is None
    assert field in row['missing_fields'] + row['invalid_fields']


def test_older_count_efold_setting_cannot_silently_set_rate_scale(monkeypatch):
    monkeypatch.setenv('COMMUNITY_RISK_EXCESS_EFOLD', '1234')
    monkeypatch.delenv('COMMUNITY_RISK_EXCESS_RATE_EFOLD', raising=False)
    assert CommunityRiskService().excess_score_efold == 10


def test_reviewed_aggregate_json_is_a_real_input_path(app, db_session, tmp_path):
    from core.db_models import Community
    db_session.add(Community(name='核验夹具社区', population=100, elderly_ratio=0.3, chronic_disease_ratio=0.2))
    db_session.commit()
    path = tmp_path / 'aggregate.json'
    payload = {'schema_version': 1, 'source_kind': 'observed_aggregate',
               'source': 'synthetic test fixture only, not a real source', 'reviewed_at': '2026-10-06',
               'profiles': [{'name': '核验夹具社区', **profile()}]}
    path.write_text(json.dumps(payload))
    app.config['COMMUNITY_RISK_PROFILE_PATH'] = str(path)
    service = CommunityRiskService()
    row = service.calculate_community_risk_score('核验夹具社区', 2)
    assert row['ranking_eligible'] and row['hazard_formula']['population_scope'] == 'all_residents'
    assert '提供方声明' in service.community_profile_status['message']
    before = service.get_ranking_input_signature()
    payload['profiles'][0]['baseline_period_days'] = 2
    path.write_text(json.dumps(payload))
    assert service.get_ranking_input_signature() != before
    payload['profiles'][0].pop('baseline_population_scope')
    path.write_text(json.dumps(payload))
    service._load_community_profiles()
    assert service.community_profiles == {}
    assert service.community_profile_status['code'] == 'aggregate_profile_invalid'


@pytest.mark.parametrize('field,value', [('source_kind', 'synthetic'), ('source', ''), ('reviewed_at', 'not-a-date')])
def test_aggregate_metadata_fails_closed(app, db_session, tmp_path, field, value):
    from core.db_models import Community
    db_session.add(Community(name='仅测试', population=100))
    db_session.commit()
    payload = {'schema_version': 1, 'source_kind': 'observed_aggregate', 'source': 'synthetic fixture',
               'reviewed_at': '2026-10-06', 'profiles': [{'name': '仅测试', **profile()}], field: value}
    path = tmp_path / 'invalid.json'; path.write_text(json.dumps(payload))
    app.config['COMMUNITY_RISK_PROFILE_PATH'] = str(path)
    assert CommunityRiskService().community_profiles == {}


def test_sensitivity_reports_count_vs_rate_and_rejects_missing_rate_denominator():
    payload = synthetic_payload()
    result = analyze(payload)
    assert result['schema_version'] == '2.0'
    assert result['count_vs_rate']['kendall_tau_b'] < 1
    assert result['ranking_rate_unit'] == '人次/千居民/日'
    payload['rankings'][0]['hazard_formula'].pop('population')
    assert analyze(payload)['excluded_missing_rows'] == 1


def test_overflow_never_escapes_as_infinite_rate():
    service = CommunityRiskService()
    service.community_profiles = {'a': {**profile(), 'population': 1e308, 'baseline_visits': 1e308}}
    result = service.calculate_community_risk_score('a', 1e308)
    assert result['ranking_eligible'] is False
    assert result['risk_score'] is None
    json.dumps(result, allow_nan=False)


def test_rate_scale_change_invalidates_cached_rankings():
    service = CommunityRiskService()
    before = service.get_ranking_input_signature()
    service.excess_score_efold = 20
    assert service.get_ranking_input_signature() != before


def test_unknown_weather_summary_respects_authorized_community_scope():
    service = CommunityRiskService()
    service.community_profiles = {'mine': profile(), 'other': profile(900)}
    result = service.generate_community_risk_map({}, community_scope={'mine'})
    assert result['summary']['total_communities'] == 1
    assert result['summary']['unranked_communities'] == 1
    assert result['rankings'] == []


def test_rates_and_history_keep_authorized_community_scope(monkeypatch):
    service = CommunityRiskService()
    service.community_profiles = {'mine': profile(), 'other': profile(900)}
    monkeypatch.setattr('services.dlnm_risk_service.get_dlnm_service',
                        lambda: type('Model', (), {'calculate_rr': lambda *a, **k: (2, {})})())
    scopes = []
    def counts(*args, **kwargs):
        scopes.append(kwargs['community_scope'])
        return {'counts_by_community': {}, 'window_days': 30, 'matched_records': 0,
                'total_records': 0, 'unmatched_records': 0}
    monkeypatch.setattr(service, '_collect_medical_counts', counts)
    result = service.generate_community_risk_map({'temperature': 35}, community_scope={'mine'})
    assert [row['community'] for row in result['rankings']] == ['mine']
    assert scopes == [{'mine'}]
