"""机构权限必须由持久化成员关系授予，系统管理员也不能绕过。"""
from flask import abort, current_app
from core.extensions import db
from core.pilot_models import PilotInstitution, PilotMembership

PERMISSIONS = {
    'uploader': {'read', 'upload', 'export'},
    'researcher': {'read', 'upload', 'export', 'model'},
    'manager': {'read', 'upload', 'export', 'model', 'manage'},
}


def require_access(institution_id, user, permission='read'):
    if not current_app.config.get('FEATURE_INSTITUTION_WORKBENCH', False):
        abort(404)
    if not user or not getattr(user, 'is_authenticated', False) or getattr(user, 'deleted_at', None):
        abort(401)
    institution = db.session.get(PilotInstitution, str(institution_id))
    member = PilotMembership.query.filter_by(institution_id=str(institution_id), user_id=user.id, active=True).first()
    if not institution or not institution.enabled or not member or permission not in PERMISSIONS.get(member.role, set()):
        abort(404)
    return institution
