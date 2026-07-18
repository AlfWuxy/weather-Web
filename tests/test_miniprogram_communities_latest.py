# -*- coding: utf-8 -*-


def test_public_communities_uses_latest_date_then_highest_id(app, db_session):
    from core.db_models import Community, Pair, User
    from core.extensions import db
    from core.security import hash_short_code
    from services.miniprogram_service import public_communities_payload

    # 当前模型有唯一约束；这里模拟升级前遗留的同日重复行，验证查询仍确定性取最大 id。
    db_session.execute(db.text("DROP TABLE community_daily"))
    db_session.execute(db.text("""
        CREATE TABLE community_daily (
            id INTEGER NOT NULL PRIMARY KEY,
            community_code VARCHAR(100) NOT NULL,
            date DATE NOT NULL,
            total_people INTEGER,
            confirm_rate FLOAT,
            escalation_rate FLOAT,
            risk_distribution TEXT,
            outreach_summary TEXT,
            created_at DATETIME,
            updated_at DATETIME
        )
    """))
    db_session.add_all([
        Community(name="甲社区", location="都昌县"),
        Community(name="乙社区", location="都昌县"),
        Community(name="丙社区", location="都昌县"),
    ])
    for community_index, community_code in enumerate(("甲社区", "乙社区"), start=1):
        for pair_index in range(5):
            owner = User(
                username=f"community-latest-owner-{community_index}-{pair_index}",
                role="caregiver",
            )
            owner.set_password("test-password")
            db_session.add(owner)
            db_session.flush()
            short_code = f"81{community_index}{pair_index:05d}"
            db_session.add(
                Pair(
                    caregiver_id=owner.id,
                    community_code=community_code,
                    location_query="都昌县",
                    elder_code=f"latest-{community_index}-{pair_index}",
                    short_code=short_code,
                    short_code_hash=hash_short_code(short_code),
                    status="active",
                )
            )
    db_session.execute(
        db.text("""
            INSERT INTO community_daily (
                id, community_code, date, total_people, confirm_rate, escalation_rate
            ) VALUES (
                :id, :community_code, :date, :total_people, :confirm_rate, :escalation_rate
            )
        """),
        [
            {
                "id": 10,
                "community_code": "甲社区",
                "date": "2026-07-17",
                "total_people": 6,
                "confirm_rate": 0.6,
                "escalation_rate": 0.1,
            },
            {
                "id": 20,
                "community_code": "甲社区",
                "date": "2026-07-18",
                "total_people": 5,
                "confirm_rate": 0.5,
                "escalation_rate": 0.2,
            },
            {
                "id": 30,
                "community_code": "甲社区",
                "date": "2026-07-18",
                "total_people": 8,
                "confirm_rate": 0.8,
                "escalation_rate": 0.3,
            },
            {
                "id": 40,
                "community_code": "甲社区",
                "date": "2026-07-16",
                "total_people": 99,
                "confirm_rate": 0.99,
                "escalation_rate": 0.99,
            },
            {
                "id": 50,
                "community_code": "乙社区",
                "date": "2026-07-18",
                "total_people": 2,
                "confirm_rate": 1.0,
                "escalation_rate": 0.5,
            },
        ],
    )
    db_session.commit()

    payload = public_communities_payload()
    by_name = {item["name"]: item for item in payload["items"]}

    latest = by_name["甲社区"]["latest_action_summary"]
    assert latest == {
        "date": "2026-07-18",
        "total_people": 5,
        "confirm_rate": 0.8,
        "escalation_rate": 0.3,
        "sample_suppressed": False,
    }
    assert by_name["乙社区"]["latest_action_summary"] == {
        "date": "2026-07-18",
        "total_people": None,
        "confirm_rate": None,
        "escalation_rate": None,
        "sample_suppressed": True,
    }
    assert by_name["丙社区"]["latest_action_summary"] is None
    assert payload["summary"] == {"community_count": 3, "scope": "都昌县"}
