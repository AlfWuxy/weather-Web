# -*- coding: utf-8 -*-
"""用户可见指标的统一解释目录。

页面浮层与透明度说明页共用 `data/content/metric_explanations.json`。
公式必须与运行代码保持一致，避免同一个指标在不同页面出现不同解释。
"""
import json
from functools import lru_cache
from pathlib import Path

_CATALOG_PATH = Path(__file__).resolve().parents[1] / 'data' / 'content' / 'metric_explanations.json'


@lru_cache(maxsize=1)
def _load_catalog():
    payload = json.loads(_CATALOG_PATH.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('metric_explanations.json must be an object')
    groups = payload.get('groups')
    metrics = payload.get('metrics')
    if not isinstance(groups, list) or not isinstance(metrics, dict):
        raise ValueError('metric_explanations.json must include groups and metrics')
    return groups, metrics


METRIC_EXPLANATION_GROUPS, METRIC_EXPLANATIONS = _load_catalog()


def get_metric_explanations():
    """返回模板使用的指标解释目录。"""
    return METRIC_EXPLANATIONS


def get_metric_explanation_groups():
    """返回透明度页使用的分组。"""
    return METRIC_EXPLANATION_GROUPS
