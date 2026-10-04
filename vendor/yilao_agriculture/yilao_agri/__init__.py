"""宜老农业：有来源约束下的农事排程与复盘。"""

__version__ = "0.2.0"


def plan(request):
    """按 JSON 输入生成可追溯安排。"""
    from .engine import plan as run
    return run(request)
