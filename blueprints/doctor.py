# -*- coding: utf-8 -*-
"""医生工作台：内容审核发布与按需接手。"""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from core.extensions import db
from core.guest import is_guest_user
from services.advice_content_service import (
    AdviceContentError,
    create_draft,
    list_advice,
    publish_advice,
    serialize_advice,
    withdraw_advice,
)
from services.help_request_service import list_help_requests

bp = Blueprint('doctor', __name__, url_prefix='/doctor')


def _require_doctor():
    if not getattr(current_user, 'is_authenticated', False) or is_guest_user(current_user):
        abort(404)
    if getattr(current_user, 'role', None) != 'doctor':
        abort(404)


@bp.before_request
def protect_doctor_area():
    _require_doctor()


@bp.route('/help', endpoint='help_inbox')
@login_required
def doctor_help_inbox():
    data = list_help_requests(current_user, status='open', requested_support_role='doctor')
    return render_template('doctor_inbox.html', data=data)


@bp.route('/content', endpoint='content_list')
@login_required
def doctor_content_list():
    rows = [serialize_advice(item, viewer=current_user) for item in list_advice(current_user)]
    return render_template('doctor_content_list.html', items=rows)


@bp.route('/content/new', methods=['GET', 'POST'], endpoint='content_new')
@login_required
def doctor_content_new():
    if request.method == 'POST':
        try:
            row = create_draft(
                current_user,
                kind=request.form.get('kind') or 'template',
                title=request.form.get('title'),
                body=request.form.get('body'),
                source=request.form.get('source'),
                scenario=request.form.get('scenario') or 'heat',
            )
            if request.form.get('publish') == 'on':
                publish_advice(current_user, row.public_id)
            db.session.commit()
            flash('内容已保存。未由医生本人发布前，不会标注为医生建议。', 'success')
            return redirect(url_for('doctor.content_list'))
        except AdviceContentError as exc:
            db.session.rollback()
            flash(exc.message, 'error')
    return render_template('doctor_content_edit.html')


@bp.route('/content/<public_id>/publish', methods=['POST'], endpoint='content_publish')
@login_required
def doctor_content_publish(public_id):
    try:
        publish_advice(current_user, public_id)
        db.session.commit()
        flash('已发布。只有你本人审核的版本可以标注医生建议。', 'success')
    except AdviceContentError as exc:
        db.session.rollback()
        flash(exc.message, 'error')
    return redirect(url_for('doctor.content_list'))


@bp.route('/content/<public_id>/withdraw', methods=['POST'], endpoint='content_withdraw')
@login_required
def doctor_content_withdraw(public_id):
    try:
        withdraw_advice(current_user, public_id)
        db.session.commit()
        flash('已撤回，自动提醒不再使用该版本。', 'success')
    except AdviceContentError as exc:
        db.session.rollback()
        flash(exc.message, 'error')
    return redirect(url_for('doctor.content_list'))
