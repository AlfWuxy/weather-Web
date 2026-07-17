# -*- coding: utf-8 -*-
"""启动入口与 CI 选择策略回归测试。"""
from pathlib import Path

import core.app as core_app


ROOT_DIR = Path(__file__).resolve().parents[1]


class _StubApp:
    def __init__(self):
        self.config = {'DEBUG': False}
        self.run_kwargs = None

    def run(self, **kwargs):
        self.run_kwargs = kwargs


def test_main_reuses_existing_app_and_preserves_default_host(monkeypatch):
    app = _StubApp()
    ready_apps = []

    monkeypatch.setattr(
        core_app,
        'create_app',
        lambda: (_ for _ in ()).throw(AssertionError('不应重复创建应用')),
    )
    monkeypatch.setattr(core_app, 'ensure_db_ready', ready_apps.append)
    monkeypatch.delenv('FLASK_HOST', raising=False)
    monkeypatch.delenv('FLASK_PORT', raising=False)

    core_app.main(app)

    assert ready_apps == [app]
    assert app.run_kwargs == {
        'debug': False,
        'host': '0.0.0.0',
        'port': 5000,
    }


def test_public_entrypoint_passes_prebuilt_app_to_main():
    source = (ROOT_DIR / 'app.py').read_text(encoding='utf-8')

    assert "if __name__ == '__main__':\n    main(app)" in source


def test_network_marker_is_registered():
    pytest_config = (ROOT_DIR / 'pytest.ini').read_text(encoding='utf-8')

    assert 'network: tests that require live third-party network access' in pytest_config
