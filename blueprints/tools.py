# -*- coding: utf-8 -*-
"""Tooling and prediction pages."""
from flask import Blueprint, current_app, render_template
from flask_login import login_required

from core.db_models import Community

bp = Blueprint('tools', __name__)


@bp.route('/ml-prediction', endpoint='ml_prediction')
@login_required
def ml_prediction():
    """ML预测页面"""
    communities = Community.query.all()
    return render_template('ml_prediction.html', communities=communities)


@bp.route('/ai-qa', endpoint='ai_qa')
@login_required
def ai_qa():
    """AI问答页面"""
    models = current_app.config.get('AI_ALLOWED_MODELS', [])
    return render_template('ai_question.html', models=models)


@bp.route('/forecast-7day', endpoint='forecast_7day')
@login_required
def forecast_7day():
    """7天健康预测页面"""
    return render_template('forecast_7day.html')


@bp.route('/chronic-risk', endpoint='chronic_risk')
@login_required
def chronic_risk():
    """慢病风险预测页面"""
    return render_template('chronic_risk.html')
