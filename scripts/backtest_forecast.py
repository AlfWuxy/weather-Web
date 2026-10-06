"""滚动起点验证入口；只读日聚合数据，不将历史实况当作历史预报。"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.model_validation_service import (DEFAULT_REPORT, EXPLORATORY_REPORT, rolling_origin_report,
    retrospective_exploratory_report, unavailable_report, _validate_exploratory_report)


def backtest(daily_path=None, predictions_path=None, output_path=None, outcome="cases_60plus", *, mode="strict", coverage_path=None):
    if mode not in {"strict", "retrospective_exploratory"}:
        raise ValueError("未知验证模式")
    if mode == "retrospective_exploratory":
        if not daily_path or predictions_path:
            raise ValueError("探索模式需要日聚合快照，不接受伪造的事前归档")
        raw = Path(daily_path).read_bytes()
        frame = pd.read_csv(daily_path)
        provenance = {"daily_file": Path(daily_path).name, "daily_sha256": hashlib.sha256(raw).hexdigest(),
                      "source_contract": "upstream cases_60plus；仅快照记录计数，未独立重算或修复",
                      "service_sha256": hashlib.sha256((Path(__file__).resolve().parents[1] / "services/model_validation_service.py").read_bytes()).hexdigest(),
                      "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        if coverage_path:
            ledger = pd.read_csv(coverage_path)
            required = {"date", "outcome", "observed_count", "eligible_primary"}
            if not required.issubset(ledger.columns) or ledger.date.duplicated().any() or set(ledger.outcome) != {outcome}:
                raise ValueError("覆盖台账结构或结局不匹配")
            compare = frame[["date", outcome]].merge(ledger[["date", "observed_count"]], on="date", how="outer", validate="one_to_one")
            if compare.isna().any().any() or not (compare[outcome] == compare.observed_count).all():
                raise ValueError("覆盖台账与聚合快照的日期或记录数不匹配")
            provenance.update(coverage_ledger_file=Path(coverage_path).name,
                              coverage_ledger_sha256=hashlib.sha256(Path(coverage_path).read_bytes()).hexdigest(),
                              ledger_rows=len(ledger),
                              ledger_eligible_primary_n=int(ledger.eligible_primary.astype(str).str.lower().isin(["true", "1", "yes"]).sum()))
        report = retrospective_exploratory_report(frame, outcome=outcome, provenance=provenance)
        _validate_exploratory_report(report)
    elif daily_path is None:
        report = unavailable_report("请提供已确认覆盖的日聚合 CSV；旧全样本 profile 回代不能作为时间外验证")
    else:
        report = rolling_origin_report(pd.read_csv(daily_path), outcome=outcome,
            predictions=pd.read_csv(predictions_path) if predictions_path else None)
    path = Path(output_path or (EXPLORATORY_REPORT if mode == "retrospective_exploratory" else DEFAULT_REPORT))
    path.parent.mkdir(parents=True, exist_ok=True)
    # 只输出聚合成绩；不公开逐日低计数健康样本。
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)
    return path, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daily", help="日聚合CSV，含date、结局、coverage_confirmed")
    parser.add_argument("--predictions", help="同结局、固定提前期的事前预测归档CSV")
    parser.add_argument("--outcome", default="cases_60plus")
    parser.add_argument("--mode", choices=["strict", "retrospective_exploratory"], default="strict")
    parser.add_argument("--coverage-ledger", help="探索模式可选的覆盖台账；只核对快照一致性，不授予覆盖确认")
    parser.add_argument("--output", help="默认严格结果写tmp，探索聚合结果写static/data")
    args = parser.parse_args()
    path, report = backtest(args.daily, args.predictions, args.output, args.outcome, mode=args.mode, coverage_path=args.coverage_ledger)
    print(f"{report['status']}: n={report['n']}; {path}")


if __name__ == "__main__":
    main()
