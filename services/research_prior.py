"""研究用系数层正态近似更新；不训练生产模型，也不生成可上线曲线。"""
import numpy as np
from datetime import date


CONTRACT_FIELDS = ("outcome", "age_group", "exposure_unit", "basis_id", "lag_days", "reference_temperature")


def update_coefficient_prior(prior, local):
    """先验和本地估计须同结局、同基函数；死亡率先验不能直接更新门诊曲线。"""
    for field in CONTRACT_FIELDS:
        if field not in prior or field not in local or prior[field] != local[field]:
            raise ValueError(f"先验与本地契约不一致：{field}")
    if not prior.get("source_doi") or prior.get("reviewed") is not True:
        raise ValueError("先验须有文献来源及人工系数审核")
    if local.get("coverage_confirmed") is not True or not local.get("training_end"):
        raise ValueError("本地覆盖与训练截止尚未确认")
    date.fromisoformat(local["training_end"])
    arrays = []
    for item in (prior, local):
        mean = np.asarray(item["coefficients"], dtype=float)
        covariance = np.asarray(item["covariance"], dtype=float)
        if mean.ndim != 1 or not len(mean) or covariance.shape != (len(mean), len(mean)):
            raise ValueError("系数与协方差维度不匹配")
        if not np.isfinite(mean).all() or not np.isfinite(covariance).all() or not np.allclose(covariance, covariance.T):
            raise ValueError("系数须有限，协方差须对称")
        try:
            np.linalg.cholesky(covariance)
        except np.linalg.LinAlgError as exc:
            raise ValueError("协方差必须正定") from exc
        eigenvalues = np.linalg.eigvalsh(covariance)
        if eigenvalues.min() < 1e-12 or eigenvalues.max() > 1e12 or np.linalg.cond(covariance) > 1e12:
            raise ValueError("协方差尺度或条件数无法稳定更新，请重新缩放系数")
        arrays.append((mean, covariance))
    (prior_mean, prior_cov), (local_mean, local_cov) = arrays
    if prior_mean.shape != local_mean.shape:
        raise ValueError("先验与本地系数维数不同")
    p0, p1 = np.linalg.inv(prior_cov), np.linalg.inv(local_cov)
    covariance = np.linalg.inv(p0 + p1)
    mean = covariance @ (p0 @ prior_mean + p1 @ local_mean)
    if not np.isfinite(mean).all() or not np.isfinite(covariance).all():
        raise ValueError("更新结果非有限，不能生成研究结果")
    np.linalg.cholesky(covariance)
    return {"status": "research_only", "production_ready": False,
            "coefficients": mean.tolist(), "covariance": covariance.tolist(),
            "contract": {key: prior[key] for key in CONTRACT_FIELDS},
            "limitations": ["系数层正态近似，不是原始计数层完整贝叶斯DLNM", "须独立复现、先验敏感性分析及时间外验证"]}
