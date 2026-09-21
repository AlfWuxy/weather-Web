"""原站稳定薄加载器：只委托显式源码目录内的注册器，不复制业务接线。"""
from importlib import import_module
from importlib.machinery import PathFinder
from pathlib import Path
import sys


def _source_paths(paths, source):
    """导入前后核对来源，拒绝同名外部包、符号链接和不可追溯的模块。"""
    if not paths:
        raise ValueError("源码模块来源无法确认，请在独立进程加载指定目录")
    for value in paths:
        path = Path(value)
        if not path.is_absolute() or path != path.resolve() or not path.is_relative_to(source):
            raise ValueError("源码模块命名冲突或包含符号链接，请使用指定源码目录")


def _loaded_sources(source):
    for name, module in tuple(sys.modules.items()):
        if name.split(".")[0] not in {"integration", "yilao_agri"}:
            continue
        filename = getattr(module, "__file__", None)
        paths = ([filename] if filename else []) + list(getattr(module, "__path__", []))
        _source_paths(paths, source)


def register_agriculture_workbench(app, *, embed_template=None):
    """关闭时不导入交付代码；开启后由唯一注册器处理身份、存储与天气回调。"""
    app.config.setdefault("YILAO_AGRICULTURE_WORKBENCH_ENABLED", False)
    if app.config["YILAO_AGRICULTURE_WORKBENCH_ENABLED"] is not True:
        return None
    value = app.config.get("YILAO_AGRICULTURE_SOURCE_ROOT")
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError("YILAO_AGRICULTURE_SOURCE_ROOT需要显式绝对目录")
    source = Path(value)
    if source != source.resolve() or not source.is_dir():
        raise ValueError("源码目录须已存在，不能包含符号链接或路径跳转")
    package = source / "integration"
    entry = package / "weather_site_registration.py"
    if (not package.is_dir() or package != package.resolve()
            or not entry.is_file() or entry != entry.resolve()):
        raise ValueError("指定目录缺少可直接加载的真实注册器，或注册器包含符号链接")
    _loaded_sources(source)
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    if "integration" not in sys.modules:
        # 本交付使用命名空间包；别处的同名普通包可能抢先匹配，必须在执行前拒绝。
        specification = PathFinder.find_spec("integration", sys.path)
        if specification is None:
            raise ValueError("指定源码中的integration包无法定位")
        origins = list(specification.submodule_search_locations or [])
        if specification.origin is not None:
            origins.append(specification.origin)
        _source_paths(origins, source)
    module = import_module("integration.weather_site_registration")
    _loaded_sources(source)
    filename = getattr(module, "__file__", None)
    if not isinstance(filename, str) or Path(filename) != entry:
        raise ValueError("实际加载的注册器与指定源码文件不一致")
    register = getattr(module, "register_agriculture_workbench", None)
    if not callable(register):
        raise ValueError("指定源码未提供有效的农业工作台注册器")
    return register(app, embed_template=embed_template)
