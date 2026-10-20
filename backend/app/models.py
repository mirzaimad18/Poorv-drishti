"""Database tables for the synthetic forward-supply-chain prototype."""

from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Depot(Base):
    __tablename__ = "depots"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    altitude_m: Mapped[int] = mapped_column(Integer)
    location: Mapped[str] = mapped_column(Geometry("POINT", srid=4326))


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    altitude_m: Mapped[int] = mapped_column(Integer)
    location: Mapped[str] = mapped_column(Geometry("POINT", srid=4326))


class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    unit: Mapped[str] = mapped_column(String(24))


class StockLevel(Base):
    __tablename__ = "stock_levels"
    __table_args__ = (
        CheckConstraint(
            "(depot_id IS NOT NULL AND post_id IS NULL) OR "
            "(depot_id IS NULL AND post_id IS NOT NULL)",
            name="stock_one_location",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    depot_id: Mapped[int | None] = mapped_column(ForeignKey("depots.id"))
    post_id: Mapped[int | None] = mapped_column(ForeignKey("posts.id"))
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    quantity: Mapped[float] = mapped_column(Float, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ConsumptionHistory(Base):
    __tablename__ = "consumption_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    day: Mapped[datetime] = mapped_column(DateTime, index=True)
    quantity: Mapped[float] = mapped_column(Float)
    temperature_c: Mapped[float] = mapped_column(Float)


class Vehicle(Base):
    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    vehicle_type: Mapped[str] = mapped_column(String(40))
    capacity_kg: Mapped[int] = mapped_column(Integer)
    available: Mapped[bool] = mapped_column(Boolean, default=True)


class RoadSegment(Base):
    __tablename__ = "road_segments"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    start_post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"))
    end_post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"))
    geometry: Mapped[str] = mapped_column(Geometry("LINESTRING", srid=4326))
    status: Mapped[str] = mapped_column(String(12), default="open")
    closure_risk: Mapped[float] = mapped_column(Float, default=0)


class WeatherEvent(Base):
    __tablename__ = "weather_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    severity: Mapped[str] = mapped_column(String(20))
    starts_at: Mapped[datetime] = mapped_column(DateTime)
    ends_at: Mapped[datetime] = mapped_column(DateTime)
    affected_post_id: Mapped[int | None] = mapped_column(ForeignKey("posts.id"))
    description: Mapped[str] = mapped_column(Text)


class DispatchPlan(Base):
    __tablename__ = "dispatch_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"))
    depot_id: Mapped[int] = mapped_column(ForeignKey("depots.id"))
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    quantity: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(24), default="proposed")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int | None] = mapped_column(ForeignKey("posts.id"))
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"))
    severity: Mapped[str] = mapped_column(String(20))
    message: Mapped[str] = mapped_column(Text)
    suggestion: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(60), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(80))
    details: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
