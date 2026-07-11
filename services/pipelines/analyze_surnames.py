# -*- coding: utf-8 -*-
"""
分析姓氏和社区分配情况
"""
from collections import Counter
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    # 兼容旧定时任务和手工命令从任意目录直接执行脚本。
    sys.path.insert(0, str(ROOT_DIR))

from core.app import create_app  # noqa: E402
from core.db_models import MedicalRecord  # noqa: E402
from core.extensions import db  # noqa: E402

app = create_app(register_blueprints=False)


def main():
    with app.app_context():
        # 获取所有病历记录
        records = MedicalRecord.query.all()

        print("=" * 80)
        print("姓氏统计（前20名）:")
        print("=" * 80)

        # 统计姓氏
        surnames = []
        for record in records:
            if record.patient_name and len(record.patient_name) > 0:
                surname = record.patient_name[0]
                surnames.append(surname)

        surname_count = Counter(surnames)
        total = len(surnames)

        for surname, count in surname_count.most_common(20):
            percentage = (count / total * 100) if total > 0 else 0
            print(f"{surname}姓：{count}人 ({percentage:.1f}%)")

        print("\n" + "=" * 80)
        print("社区分配情况:")
        print("=" * 80)

        # 统计社区分布
        community_dist = Counter([r.community for r in records if r.community])
        for community, count in community_dist.most_common():
            percentage = (count / len(records) * 100) if records else 0
            print(f"{community}: {count}条 ({percentage:.1f}%)")

        print("\n" + "=" * 80)
        print("未分配社区的姓氏（映射表中没有的）:")
        print("=" * 80)

        # 映射表中已有的姓氏
        mapped_surnames = {'周', '徐', '谭', '汪', '段', '吴', '邵', '伍', '付'}

        # 找出未映射的姓氏
        unmapped_surnames = {}
        for record in records:
            if record.patient_name and len(record.patient_name) > 0:
                surname = record.patient_name[0]
                if surname not in mapped_surnames:
                    if surname not in unmapped_surnames:
                        unmapped_surnames[surname] = []
                    unmapped_surnames[surname].append(record.community)

        # 统计未映射姓氏的数量和分配的社区
        unmapped_stats = {}
        for surname, communities in unmapped_surnames.items():
            count = len(communities)
            community = communities[0] if communities else '未知'
            unmapped_stats[surname] = {'count': count, 'community': community}

        # 按数量排序
        sorted_unmapped = sorted(
            unmapped_stats.items(),
            key=lambda x: x[1]['count'],
            reverse=True
        )

        total_unmapped = sum(s['count'] for s in unmapped_stats.values())

        for surname, info in sorted_unmapped[:20]:
            percentage = (info['count'] / total * 100) if total > 0 else 0
            print(
                f"{surname}姓：{info['count']}人 ({percentage:.1f}%) - 已分配到: {info['community']}"
            )

        print(f"\n未映射姓氏总数: {total_unmapped}人")
        print(f"未映射姓氏种类: {len(unmapped_stats)}个")


if __name__ == '__main__':
    main()
