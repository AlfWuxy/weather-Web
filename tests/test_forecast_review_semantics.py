"""复核核心温度缺失与上游评分来源的语义边界。"""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from services.forecast_service import ForecastService
from services.weather_service import WeatherService


@pytest.fixture
def forecast():
    return ForecastService.__new__(ForecastService)


@pytest.mark.parametrize('temperature', [None, float('nan'), float('inf'), 'bad'])
@pytest.mark.parametrize('other', [dict(humidity=70), dict(humidity=95, pm25=200, temp_min=29)])
def test_temperature_unknown_prevents_overall_exposure_classification(forecast, temperature, other):
    result = forecast._composite_exposure_risk(temperature, **{'temp_min': None, **other})
    assert result['score'] is result['pre_clip_score'] is result['final_score'] is None
    assert result['level'] == '未知'
    assert result['status'] == 'unknown'
    assert result['missing_required_inputs'] == ['temperature']
    assert result['components']['heat'] is None
    assert result['components']['humidity'] is not None
    assert result['inputs']['temperature']['status'] == 'unknown'
    assert 'heat' in result['unknown_components']


@pytest.mark.parametrize('temperature', [0, -10, 35])
def test_finite_temperature_keeps_valid_zero_and_negative_values(forecast, temperature):
    result = forecast._composite_exposure_risk(temperature, None, 70)
    assert result['score'] is not None
    assert result['missing_required_inputs'] == []
    assert result['inputs']['temperature']['used_value'] == temperature


@pytest.mark.parametrize('score', [0, 75, 100])
@pytest.mark.parametrize('spread,count', [(None, 1), (float('nan'), 3), (-1, 3), (0, 1), (0, 3)])
def test_valid_external_score_is_used_and_marked_unvalidated(forecast, score, spread, count):
    result = forecast._calculate_predictability(4, spread, count, score,
        external_source='provider_reference', external_method='provider_index')
    assert result['score'] == score
    assert result['branch'] == 'external'
    assert result['source'] == 'provider_reference'
    assert result['method'] == 'provider_index'
    assert result['validated'] is False
    assert result['calibration_status'] == 'unvalidated'
    assert result['inputs']['external_score_status'] == 'valid'
    assert result['inputs']['lead_penalty'] is None
    assert '校准情况待核实' in result['note']
    assert '分数与实际预报误差的对应关系待验证' in result['limitations']


@pytest.mark.parametrize('external', [None, float('nan'), float('inf'), -1, 101, True, 'bad'])
@pytest.mark.parametrize('spread,count,branch,expected', [(None, 2, 'lead_only', 60), (0, 1, 'lead_only', 60), (0, 2, 'derived', 99), (1.5, 5, 'derived', 76)])
def test_missing_or_invalid_external_scores_have_explicit_fallback(forecast, external, spread, count, branch, expected):
    result = forecast._calculate_predictability(1, spread, count, external)
    assert result['branch'] == branch
    assert result['score'] == expected
    assert result['inputs']['external_score_status'] == ('missing' if external is None else 'invalid')
    if external is not None:
        assert result['inputs']['external_score_rejection']


def test_local_weather_upstream_is_not_mislabelled_external(forecast):
    weather = WeatherService()
    entry = weather._merge_multimodel_forecast([
        {'date': '2026-10-06', 'temperature_mean': 30, 'temperature_max': 35, 'temperature_min': 25}
    ], [])[0]
    normalized = forecast._normalize_forecast_entry(entry)
    result = forecast._calculate_predictability(1, normalized['model_spread'], normalized['model_count'],
        normalized['predictability_score'], external_source=normalized['predictability_source'],
        external_method=normalized['predictability_method'])
    assert result['branch'] == 'lead_only'
    assert result['score'] == 60
    assert result['source'] == 'local_forecast_service'


def test_external_metadata_survives_full_seven_day_prediction(forecast, monkeypatch):
    from services import dlnm_risk_service
    forecast.weather_history = None
    forecast.qm_params = {}
    forecast.visit_mean = forecast.visit_threshold_p90 = None
    forecast.max_observed_daily_visits = None
    monkeypatch.setattr(dlnm_risk_service, 'get_dlnm_service', lambda: SimpleNamespace(identify_extreme_weather_events=lambda *_a: []))
    days, summary = forecast.generate_7day_forecast([
        {'date': (date(2026, 10, 6) + timedelta(days=i)).isoformat(), 'temperature': 30,
         'predictability_score': 0, 'predictability_source': 'provider_reference', 'predictability_method': 'index'}
        for i in range(7)
    ], start_date=date(2026, 10, 6))
    assert summary['predictability']['average_score'] == 0
    assert all(day['predictability']['branch'] == 'external' for day in days)
    assert all(day['predictability']['source'] == 'provider_reference' for day in days)


def _nowcast(monkeypatch, hourly, units=None, timezone='Asia/Shanghai'):
    from services import weather_service as module
    calls = []
    payload = {'hourly': hourly, 'hourly_units': units or {}, 'timezone': timezone}
    def get(*_a, **kwargs):
        calls.append(kwargs['params'])
        return SimpleNamespace(status_code=200, json=lambda: payload)
    monkeypatch.setattr(module.requests, 'get', get)
    monkeypatch.setattr(module, '_record_external_api_timing', lambda *_a: None)
    result = WeatherService().get_short_term_nowcast(hours=len(hourly['time']))
    return result, calls[0]


def test_hourly_rainfall_amount_is_separate_from_snow_and_probability(monkeypatch):
    result, params = _nowcast(monkeypatch, {
        'time': ['2026-10-06T12:00', '2026-10-06T13:00'],
        'rain': [1, 0], 'showers': [2, 0], 'precipitation': [9, 5],
        'precipitation_probability': [None, 0],
    })
    assert {'rain', 'showers'} <= set(params['hourly'].split(','))
    assert params['precipitation_unit'] == 'mm'
    assert result['timezone'] == 'Asia/Shanghai'
    assert result['rainfall_interval_minutes'] == result['precipitation_interval_minutes'] == 60
    assert result['rainfall_time_reference'] == result['precipitation_time_reference'] == 'interval_end'
    assert result['timeline'][0]['rainfall_mm'] == 3
    assert result['timeline'][1]['rainfall_mm'] == 0
    assert result['timeline'][0]['precipitation_probability'] is None
    assert result['timeline'][0]['time'] == '2026-10-06T12:00+08:00'
    assert result['timeline'][0]['interval_start'] == '2026-10-06T11:00+08:00'
    assert result['rainfall_source'] == 'forecast_model'
    assert result['probability_complete'] is False


@pytest.mark.parametrize('rain,showers', [(None, 3), (2, None), (float('nan'), 2), (-1, 2)])
def test_partial_rainfall_never_inferred_from_probability(monkeypatch, rain, showers):
    result, _ = _nowcast(monkeypatch, {'time': ['2026-10-06T12:00'],
        'rain': [rain], 'showers': [showers], 'precipitation_probability': [90]})
    assert result['timeline'][0]['rainfall_mm'] is None
    assert result['timeline'][0]['precipitation_probability'] == 90


@pytest.mark.parametrize('times', [['2026-10-06T12:00', '2026-10-06T14:00'],
                                  ['2026-10-06T12:30'], ['damaged']])
def test_invalid_hourly_window_is_unavailable(monkeypatch, times):
    result, _ = _nowcast(monkeypatch, {'time': times})
    assert result['available'] is False
    assert result['timeline'] == []


def test_incompatible_amount_unit_is_unknown(monkeypatch):
    result, _ = _nowcast(monkeypatch, {'time': ['2026-10-06T12:00'],
        'rain': [1], 'showers': [2]}, units={'rain': 'inch', 'showers': 'mm'})
    assert result['timeline'][0]['rainfall_mm'] is None


def test_unexpected_timezone_is_not_mislabeled(monkeypatch):
    result, _ = _nowcast(monkeypatch, {'time': ['2026-10-06T12:00']}, timezone='GMT')
    assert result['available'] is False
    assert result['reason'] == 'unexpected_timezone'


# 用真实卡片适配器构造页面输入，单独验证默认内容与展开内容的边界。
def _render_forecast_copy(days, *, no_health=False, template='forecast_7day.html'):
    from pathlib import Path
    from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader
    from services.forecast_cards import build_forecast_cards

    start = date(2026, 10, 11)
    weather = [{'date': (start + timedelta(days=i)).isoformat(), 'data_source': 'QWeather',
                'temperature_max': 25, 'temperature_min': 15, 'humidity': 70,
                'wind_speed': 2, 'condition': '晴'} for i in range(len(days))]
    service = ForecastService.__new__(ForecastService)
    health = [{'date': item['date'], 'composite_exposure': service._composite_exposure_risk(
                    20, temp_min=15, humidity=70),
               'predictability': service._calculate_predictability(i + 1)}
              for i, item in enumerate(weather)]
    cards = build_forecast_cards(weather, [] if no_health else health, start)
    for card, overrides in zip(cards, days):
        card.update(overrides)
    env = Environment(loader=ChoiceLoader([
        DictLoader({'base.html': '{% block content %}{% endblock %}',
                    'partials/metric_info.html': '{% macro metric_info(key, context={}) %}{% endmacro %}'}),
        FileSystemLoader(str(Path(__file__).resolve().parents[1] / 'templates')),
    ]), autoescape=True)
    if template == 'user_dashboard.html':
        source = (Path(__file__).resolve().parents[1] / 'templates' / template).read_text()
        source = source[source.index('        {# 7 日预测 #}'):source.index('        {# 家人概览 #}')]
        template_obj = env.from_string('{% from "partials/metric_info.html" import metric_info %}' + source)
    else:
        template_obj = env.get_template(template)
    return template_obj.render(
        url_for=lambda *_a, **_k: '/forecast-7day',
        request=SimpleNamespace(args={}), current_location='都昌', location_options=[],
        forecast_meta={'source_label': 'QWeather'}, forecast_error=None,
        forecast_days=cards, weekly_tips=[],
    )



def _parse_forecast_copy(html):
    from html.parser import HTMLParser

    class CopyParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.details = []
            self.cards = []
            self.visible = []
            self.text = []
            self.in_details = False
            self.in_script = False
            self.has_chart = False

        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag == 'script':
                self.in_script = True
            if tag == 'details':
                self.in_details = True
                self.details.append({'attrs': values, 'text': ''})
            if tag == 'div' and values.get('data-risk') == 'attention':
                self.cards.append('')
            if tag == 'canvas' and values.get('id') == 'forecastChart':
                self.has_chart = True

        def handle_endtag(self, tag):
            if tag == 'details':
                self.in_details = False
            if tag == 'script':
                self.in_script = False

        def handle_data(self, data):
            if self.in_script:
                return
            self.text.append(data)
            if self.cards:
                self.cards[-1] += data
            if self.in_details:
                self.details[-1]['text'] += data
            else:
                self.visible.append(data)

    parser = CopyParser()
    parser.feed(html)
    parser.text = ''.join(parser.text)
    parser.visible = ''.join(parser.visible)
    return parser


def test_forecast_copy_puts_reference_scores_inside_closed_details():
    page = _parse_forecast_copy(_render_forecast_copy([{} for _ in range(7)]))
    assert len(page.details) == 7
    for i, detail in enumerate(page.details):
        assert 'open' not in detail['attrs']
        assert '结果依据' in detail['text']
        assert f'预报参考分（规则计算）：{60 - i * 3}.0' in detail['text']
        assert '按提前天数计算，最高 60 分' in detail['text']
        assert '分数与实际预报误差的对应关系待验证' in detail['text']
    assert page.visible.count('本周 PM2.5 预报缺失。') == 1
    assert page.visible.count('天气关注分 2') == 7
    assert page.visible.count('规则评分 · 低关注') == 7
    assert '预报参考分' not in page.visible
    assert '门诊' not in page.visible and '就诊' not in page.visible
    assert '本周重点关注日' not in page.visible
    assert '本周建议' not in page.visible


def test_forecast_copy_preserves_day_specific_missing_fields_and_zero():
    html = _render_forecast_copy([
        {'pm25_used': 15, 'pm25_source': 'direct', 'risk_score': 0},
        {'unknown_components': ['pm25', 'humidity'], 'risk_score': 80, 'risk_label': '高关注'},
        {'temp_high': None, 'risk_available': False, 'risk_score': None,
         'unknown_components': ['heat', 'pm25']},
    ])
    page = _parse_forecast_copy(html)
    assert '天气关注分 0' in page.cards[0]
    assert '本日 PM2.5 预报缺失' not in page.cards[0]
    assert '湿度资料缺失' in page.cards[1]
    assert '本日 PM2.5 预报缺失' in page.cards[1]
    assert '未知 / 15°' in page.cards[2]
    assert '温度资料缺失' in page.cards[2]
    assert '天气关注分未知' in page.cards[2]
    assert 'None°' not in html
    assert '本周 PM2.5 预报缺失' not in html
    assert page.has_chart
    assert '本周重点关注日' in page.text
    assert '资料缺失的日期在图中留空。' in page.text


@pytest.mark.parametrize('days', [[], [{} for _ in range(7)]])
def test_forecast_copy_hides_empty_charts_and_clinic_modules(days):
    page = _parse_forecast_copy(_render_forecast_copy(days, no_health=True))
    assert not page.has_chart
    assert page.details == []
    assert '本周重点关注日' not in page.text
    assert '本周建议' not in page.text
    assert '门诊' not in page.text and '就诊' not in page.text
    if days:
        assert page.text.count('天气关注分未知') == 7
        assert page.text.count('25° / 15°') == 7
        assert '天气关注分按其余可用分项计算。' not in page.text
    else:
        assert page.text.count('7 天预报正在更新') == 1


@pytest.mark.parametrize('branch,score,method_text', [
    ('external', 0, '采用上游提供的参考分'),
    ('derived', 99, '按多模型温差与提前天数计算'),
])
def test_forecast_copy_keeps_actual_external_and_multimodel_reference(branch, score, method_text):
    page = _parse_forecast_copy(_render_forecast_copy([{
        'predictability_branch': branch, 'predictability_score': score,
        'predictability_model_spread': 0 if branch == 'derived' else None,
    }]))
    detail = page.details[0]['text']
    assert f'预报参考分（规则计算）：{score}' in detail
    assert method_text in detail
    assert '准确率' not in detail
    if branch == 'derived':
        assert '模型温差 0°C' in detail


@pytest.mark.parametrize('no_health', [False, True])
def test_dashboard_forecast_uses_same_visible_score_and_closed_basis(no_health):
    page = _parse_forecast_copy(_render_forecast_copy(
        [{} for _ in range(7)], no_health=no_health, template='user_dashboard.html'))
    assert len(page.cards) == 7
    assert '门诊' not in page.text and '高负荷' not in page.text
    assert '7 日健康预测' not in page.text
    assert '预报参考分' not in page.visible
    assert page.visible.count('本周 PM2.5 预报缺失。') == 1
    assert page.visible.count('25° / 15°') == 7
    if no_health:
        assert page.details == []
        assert page.visible.count('天气关注分未知') == 7
    else:
        assert page.visible.count('天气关注分 2') == 7
        assert page.visible.count('规则评分 · 低关注') == 7
        assert len(page.details) == 7
        assert all('open' not in item['attrs'] for item in page.details)
        assert '预报参考分（规则计算）：60.0' in page.details[0]['text']
