"""统一输入来源与缺失值语义；未知不得伪装成零风险。"""
import math


def finite_number(value):
    """只接受有限数值，保留真实的零。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def input_state(value, *, source=None, method=None, status=None, reason=None):
    """插补必须声明方法；未知值统一置空并保留原因。"""
    missing = value is None or (isinstance(value, float) and not math.isfinite(value))
    status = status or ('unknown' if missing else 'imputed' if method else 'observed')
    if status not in {'observed', 'imputed', 'unknown'}:
        raise ValueError('invalid input status')
    if status == 'imputed' and not str(method or '').strip():
        raise ValueError('imputed inputs require a method')
    if missing:
        status = 'unknown'
    if status == 'unknown':
        value = None
    return {'value': value, 'used_value': value, 'status': status, 'source': source,
            'method': method, 'reason': reason or ('missing' if status == 'unknown' else None),
            'imputed': status == 'imputed'}


def risk_floor(candidate, baseline, *, status):
    """插补仅能维持或提高已有风险，无法比较时保持未知。"""
    candidate, baseline = finite_number(candidate), finite_number(baseline)
    if status == 'unknown' or candidate is None:
        return None
    if status == 'imputed':
        return max(candidate, baseline) if baseline is not None else None
    return candidate


def weighted_known_risk(components, weights):
    """仅归一有值分项；缺失清单必须一直传递到展示层。"""
    known = {k: finite_number(v) for k, v in components.items() if weights.get(k, 0) > 0}
    unknown = [k for k, v in known.items() if v is None]
    denominator = sum(weights[k] for k, v in known.items() if v is not None)
    effective = {k: weights[k] / denominator if v is not None and denominator else 0.0
                 for k, v in known.items()}
    score = sum(v * effective[k] for k, v in known.items() if v is not None) if denominator else None
    return {'score': score, 'effective_weights': effective, 'unknown_components': unknown,
            'status': 'unknown' if score is None else 'partial' if unknown else 'complete'}
