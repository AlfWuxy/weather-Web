# -*- coding: utf-8 -*-
"""Public project status and boundary page."""
from flask import Blueprint, render_template

from core.status_content import (
    BUILD_DATE,
    DATA_SOURCES,
    ENGLISH_SUMMARY,
    GITHUB_URL,
    NO_GO,
    ONE_LINE_ZH,
    PROTOTYPE,
    UNVERIFIED,
    VERIFIED,
    VERSION,
)

bp = Blueprint('status', __name__)


@bp.app_context_processor
def inject_status_copy():
    """Expose the boundary one-liner and GitHub URL to every template."""
    return {
        'status_one_line_zh': ONE_LINE_ZH,
        'status_github_url': GITHUB_URL,
    }


@bp.route('/status', endpoint='status')
def status_page():
    """项目状态与边界（公开，无需登录）。"""
    return render_template(
        'status.html',
        english_summary=ENGLISH_SUMMARY,
        one_line_zh=ONE_LINE_ZH,
        verified=VERIFIED,
        prototype=PROTOTYPE,
        unverified=UNVERIFIED,
        no_go=NO_GO,
        data_sources=DATA_SOURCES,
        version=VERSION,
        build_date=BUILD_DATE,
        github_url=GITHUB_URL,
    )
