"""运行 celery -A pilot_worker.celery worker / beat。"""
from core.app import create_app
from core.pilot_runtime import create_celery

app = create_app(register_blueprints=False)
celery = create_celery(app)
