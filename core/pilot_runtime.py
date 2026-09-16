"""机构试点的独立配置、请求边界和后台任务入口。"""
import os
from io import BytesIO

from flask import Request, abort, current_app, request
from flask_login import current_user


class PilotRequest(Request):
    def _get_file_stream(self, total_content_length, content_type, filename=None,
                         content_length=None):
        # 病历在加密前只能留在内存；请求总量仍由 max_content_length 限制。
        if (current_app.config.get('FEATURE_INSTITUTION_WORKBENCH')
                and self.path.startswith('/api/v1/workbench/')
                and self.mimetype == 'multipart/form-data'):
            return BytesIO()
        return super()._get_file_stream(total_content_length, content_type,
                                        filename=filename, content_length=content_length)

    @property
    def max_content_length(self):
        # 只为工作台的受鉴权上传入口扩大限额，旧站其他入口保持原限制。
        if current_app.config.get('FEATURE_INSTITUTION_WORKBENCH'):
            if self.path.startswith('/api/v1/workbench/'):
                if self.mimetype == 'multipart/form-data':
                    return 20 * 1024 * 1024
                return 2 * 1024 * 1024
        return super().max_content_length


def configure_pilot(app):
    from core import pilot_models  # noqa: F401，注册独立表供 Alembic 使用

    app.config.setdefault('FEATURE_INSTITUTION_WORKBENCH',
                          os.getenv('FEATURE_INSTITUTION_WORKBENCH', '0') == '1')
    app.config.setdefault('PILOT_STORAGE_DIR', os.getenv('PILOT_STORAGE_DIR', ''))
    app.config.setdefault('PILOT_STORAGE_KEY', os.getenv('PILOT_STORAGE_KEY', ''))
    app.config.setdefault('PILOT_REDIS_URL', os.getenv('PILOT_REDIS_URL', ''))
    app.config.setdefault('PILOT_TASKS_EAGER', False)
    app.config.setdefault('PILOT_HTTP_TIMEOUT', 30)
    app.request_class = PilotRequest

    @app.context_processor
    def pilot_context():
        accessible = False
        if app.config.get('FEATURE_INSTITUTION_WORKBENCH') and current_user.is_authenticated:
            from core.pilot_models import PilotMembership, PilotInstitution
            accessible = PilotMembership.query.join(PilotInstitution).filter(
                PilotMembership.user_id == current_user.id, PilotMembership.active.is_(True),
                PilotInstitution.enabled.is_(True)).first() is not None
        return {'workbench_accessible': accessible}

    @app.before_request
    def pilot_request_gate():
        if request.path == '/workbench' or request.path.startswith('/api/v1/workbench/'):
            if not app.config.get('FEATURE_INSTITUTION_WORKBENCH'):
                abort(404)

    @app.after_request
    def pilot_private_response(response):
        if request.path == '/workbench' or request.path.startswith('/api/v1/workbench/'):
            response.headers['Cache-Control'] = 'no-store, private, max-age=0'
            response.headers['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
            response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    from services.data_workbench.cli import register_pilot_cli
    register_pilot_cli(app)


def create_celery(app):
    """工作进程与网页共享配置，队列只传任务 ID，不传病历。"""
    from celery import Celery, Task
    from celery.schedules import crontab

    broker = app.config.get('PILOT_REDIS_URL')
    if not broker:
        raise RuntimeError('需要配置独立的 PILOT_REDIS_URL 才能启动工作进程')

    class FlaskTask(Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery = Celery(app.name, task_cls=FlaskTask, broker=broker)
    celery.conf.update(
        task_default_queue='institution-pilot',
        task_serializer='json', accept_content=['json'], result_serializer='json',
        task_ignore_result=True, task_acks_late=True, task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1, task_soft_time_limit=840, task_time_limit=900,
        broker_connection_retry_on_startup=True,
        broker_connection_timeout=3,
        broker_transport_options={'visibility_timeout': 1800},
        timezone='Asia/Shanghai', enable_utc=True,
        beat_schedule={
            'recover-pilot-jobs': {'task': 'pilot.recover', 'schedule': 60.0},
            'daily-pilot-forecasts': {'task': 'pilot.daily',
                                    'schedule': crontab(hour=8, minute=0)},
        },
    )
    from services.data_workbench.jobs import execute_job, recover_jobs, daily_forecasts
    celery.task(name='pilot.execute')(execute_job)
    celery.task(name='pilot.recover')(recover_jobs)
    celery.task(name='pilot.daily')(daily_forecasts)
    app.extensions['pilot_celery'] = celery
    return celery
