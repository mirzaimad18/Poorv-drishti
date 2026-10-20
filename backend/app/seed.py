"""Create the schema and fill it with made-up demo logistics data."""

import math
import os
import random
from datetime import datetime, timedelta

from geoalchemy2.shape import from_shape
from shapely.geometry import LineString, Point

from .db import Base, SessionLocal, engine
from .models import (
    Alert,
    ConsumptionHistory,
    Depot,
    DispatchPlan,
    Item,
    Post,
    RoadSegment,
    StockLevel,
    Vehicle,
    WeatherEvent,
    User,
)
from .security import hash_password

random.seed(26251)

# These public map coordinates describe a broad Himalayan demo area. All names,
# stock values, vehicles, events, and movement records in this project are fake.
DEPOT_DATA = [
    ("Depot North", 2800, 77.12, 34.20),
    ("Depot Central", 2450, 77.28, 34.14),
    ("Depot South", 2200, 77.43, 34.08),
]
POST_DATA = [
    ("Post Alpha", 1650, 77.17, 34.18),
    ("Post Bravo", 2100, 77.23, 34.23),
    ("Post Charlie", 2550, 77.31, 34.19),
    ("Post Delta", 2900, 77.37, 34.14),
    ("Post Echo", 3300, 77.27, 34.10),
    ("Post Foxtrot", 3650, 77.19, 34.07),
    ("Post Golf", 4050, 77.36, 34.06),
    ("Post Hotel", 4400, 77.45, 34.12),
]
ITEM_DATA = [
    ("Fuel", "litres"),
    ("Rations", "packs"),
    ("Medical kits", "kits"),
    ("Winter clothing", "sets"),
    ("Spare parts", "units"),
]


def point(lon: float, lat: float):
    """Build a PostGIS point from longitude and latitude."""
    return from_shape(Point(lon, lat), srid=4326)


def add_demo_data(session):
    """Insert one deterministic dataset; do not duplicate it on reruns."""
    seed_demo_users(session)
    if session.query(Depot).first():
        print("Demo data already exists; skipping seed.")
        return

    depots = [
        Depot(name=name, altitude_m=alt, location=point(lon, lat))
        for name, alt, lon, lat in DEPOT_DATA
    ]
    posts = [
        Post(name=name, altitude_m=alt, location=point(lon, lat))
        for name, alt, lon, lat in POST_DATA
    ]
    items = [Item(name=name, unit=unit) for name, unit in ITEM_DATA]
    vehicles = [
        Vehicle(
            name=f"Demo Vehicle {number:02}",
            vehicle_type=("cargo truck" if number <= 6 else "utility 4x4"),
            capacity_kg=(5000 if number <= 6 else 1200),
        )
        for number in range(1, 11)
    ]
    session.add_all(depots + posts + items + vehicles)
    session.flush()

    # Keep a stock row for every item at every demo depot and post.
    for depot in depots:
        for item in items:
            session.add(
                StockLevel(
                    depot_id=depot.id,
                    item_id=item.id,
                    quantity=random.randint(700, 2400),
                )
            )
    for post in posts:
        for item in items:
            session.add(
                StockLevel(
                    post_id=post.id,
                    item_id=item.id,
                    quantity=random.randint(50, 280),
                )
            )

    # Connect posts in a small, synthetic road network using straight segments.
    for index, post in enumerate(posts):
        next_post = posts[(index + 1) % len(posts)]
        start = POST_DATA[index]
        end = POST_DATA[(index + 1) % len(posts)]
        is_closed = index == 3
        session.add(
            RoadSegment(
                name=f"Demo Road {index + 1}",
                start_post_id=post.id,
                end_post_id=next_post.id,
                geometry=from_shape(
                    LineString([(start[2], start[3]), (end[2], end[3])]),
                    srid=4326,
                ),
                status="closed" if is_closed else "open",
                closure_risk=0.72 if is_closed else round(random.uniform(0.03, 0.42), 2),
            )
        )

    # Create 730 daily usage records for each post/item pair.
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    for post in posts:
        altitude_factor = 1 + (post.altitude_m - 1500) / 9000
        for item in items:
            item_factor = 2.6 if item.name == "Fuel" else 1.0
            for days_ago in range(730, 0, -1):
                day = today - timedelta(days=days_ago)
                winter_factor = 1.45 if day.month in (11, 12, 1, 2, 3) else 1.0
                baseline = item_factor * altitude_factor * winter_factor
                quantity = max(0.1, random.gauss(baseline, baseline * 0.16))
                temperature = (
                    13
                    - (post.altitude_m / 1000 * 5)
                    + 8 * math.cos((day.timetuple().tm_yday - 200) * math.tau / 365)
                    + random.uniform(-3, 3)
                )
                session.add(
                    ConsumptionHistory(
                        post_id=post.id,
                        item_id=item.id,
                        day=day,
                        quantity=round(quantity, 2),
                        temperature_c=round(temperature, 1),
                    )
                )

    now = datetime.utcnow()
    session.add_all(
        [
            WeatherEvent(
                name="Demo snowstorm",
                severity="severe",
                starts_at=now + timedelta(days=2),
                ends_at=now + timedelta(days=4),
                affected_post_id=posts[6].id,
                description="Synthetic heavy snowfall and reduced visibility.",
            ),
            WeatherEvent(
                name="Demo high winds",
                severity="moderate",
                starts_at=now + timedelta(days=5),
                ends_at=now + timedelta(days=6),
                affected_post_id=posts[3].id,
                description="Synthetic high winds may slow travel.",
            ),
        ]
    )
    session.commit()
    print(
        f"Seed complete: {len(depots)} depots, {len(posts)} posts, "
        f"{len(items)} items, {len(vehicles)} vehicles, "
        f"{len(posts) * len(items) * 730} daily usage records."
    )


def seed_demo_users(session):
    """Create local-only prototype accounts without overwriting existing credentials."""
    accounts = [
        ("planner", "planner", os.getenv("DEMO_PLANNER_PASSWORD", "planner-demo-password")),
        ("commander", "commander", os.getenv("DEMO_COMMANDER_PASSWORD", "commander-demo-password")),
        ("admin", "admin", os.getenv("DEMO_ADMIN_PASSWORD", "admin-demo-password")),
    ]
    for username, role, password in accounts:
        existing = session.query(User).filter_by(username=username).first()
        if existing is None:
            session.add(
                User(
                    username=username,
                    password_hash=hash_password(password),
                    role=role,
                    active=True,
                )
            )
    session.commit()


def main():
    """Create PostGIS tables, then seed the demo rows."""
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as session:
        add_demo_data(session)


if __name__ == "__main__":
    main()
