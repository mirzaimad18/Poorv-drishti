"""HTTP API for the synthetic Purva Drishti logistics prototype."""

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from geoalchemy2.shape import to_shape
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .db import get_db
from .forecasting import get_forecast, retrain_models
from .models import (
    Alert,
    AuditLog,
    ConsumptionHistory,
    Depot,
    DispatchPlan,
    Item,
    Post,
    RoadSegment,
    StockLevel,
    User,
    Vehicle,
    WeatherEvent,
)
from .risk import post_risk_score, supply_risk
from .routing import route_between
from .security import (
    Administrator,
    Commander,
    Planner,
    create_access_token,
    verify_password,
)
from . import mqtt

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    mqtt.start_subscriber()
    yield
    mqtt.stop_subscriber()


app = FastAPI(title="Purva Drishti API", version="1.0.0", lifespan=lifespan)
origins = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(SQLAlchemyError)
async def database_error_handler(request: Request, exc: SQLAlchemyError):
    logger.exception("Database request failed", exc_info=exc)
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Database operation failed",
            "error": f"{type(exc).__name__}: {getattr(exc, 'orig', exc)}",
        },
    )


def _record_audit(session: Session, user_id: int | None, action: str, details: dict):
    session.add(
        AuditLog(
            user_id=user_id,
            action=action,
            details=json.dumps(details, separators=(",", ":"), default=str),
        )
    )
    session.commit()


def _location(geometry):
    point = to_shape(geometry)
    return {"longitude": float(point.x), "latitude": float(point.y)}


def _safe_window_estimate(session: Session, post: Post) -> tuple[float, str]:
    now = datetime.utcnow()
    events = list(
        session.scalars(
            select(WeatherEvent).where(
                (WeatherEvent.affected_post_id == post.id)
                | WeatherEvent.affected_post_id.is_(None),
                func.lower(WeatherEvent.severity).in_(("severe", "high", "moderate")),
                WeatherEvent.ends_at >= now,
                WeatherEvent.starts_at <= now + timedelta(days=14),
            )
        )
    )
    closed_roads = list(
        session.scalars(
            select(RoadSegment).where(
                func.lower(RoadSegment.status) == "closed",
                (RoadSegment.start_post_id == post.id)
                | (RoadSegment.end_post_id == post.id),
            )
        )
    )
    delays = [
        max(0.0, (event.ends_at - now).total_seconds() / 86400)
        for event in events
    ]
    if delays:
        days = max(delays)
        details = "; ".join(
            f"{event.name} ({event.severity}) is expected to clear by "
            f"{event.ends_at.isoformat()}."
            for event in events
        )
        if closed_roads:
            details += " A connected road is marked closed; no reopening time is recorded."
        return round(days, 1), details
    if closed_roads:
        names = ", ".join(road.name for road in closed_roads)
        return (
            14.0,
            f"Closed road(s) {names} have no reopening schedule; using a conservative 14-day planning window.",
        )
    return (
        14.0,
        "No near-term severe weather or connected road closure is recorded; using the 14-day planning horizon.",
    )


def _post_summary(session: Session, post: Post) -> dict:
    stocks = session.execute(
        select(Item.id, Item.name, Item.unit, StockLevel.quantity)
        .join(StockLevel, StockLevel.item_id == Item.id)
        .where(StockLevel.post_id == post.id)
    ).all()
    consumption = session.execute(
        select(ConsumptionHistory.item_id, func.avg(ConsumptionHistory.quantity))
        .where(
            ConsumptionHistory.post_id == post.id,
            ConsumptionHistory.day >= datetime.utcnow() - timedelta(days=7),
        )
        .group_by(ConsumptionHistory.item_id)
    ).all()
    demand_by_item = {item_id: float(avg or 0) for item_id, avg in consumption}
    days_to_window, window_explanation = _safe_window_estimate(session, post)
    item_stocks = []
    risk_levels = []
    for item_id, name, unit, quantity in stocks:
        daily_demand = demand_by_item.get(item_id)
        days_stock = float(quantity) / daily_demand if daily_demand and daily_demand > 0 else None
        risk = supply_risk(days_stock, days_to_window, window_explanation)
        risk_levels.append(risk["level"])
        item_stocks.append(
            {
                "item_id": item_id,
                "item": name,
                "unit": unit,
                "quantity": float(quantity),
                "average_daily_demand_7d": round(daily_demand, 2) if daily_demand is not None else None,
                "risk": risk,
            }
        )
    return {
        "id": post.id,
        "name": post.name,
        "altitude_m": post.altitude_m,
        "location": _location(post.location),
        "stocks": item_stocks,
        "risk": {
            "days_to_next_safe_window": days_to_window,
            "explanation": window_explanation,
            "critical_items": sum(level == "Critical" for level in risk_levels),
        },
        "risk_score": post_risk_score([stock["risk"] for stock in item_stocks]),
    }


def _all_plan_metadata(session: Session) -> dict[int, dict]:
    # Existing audit schema is retained; plan-specific fields live in its JSON details.
    candidates = session.scalars(
        select(AuditLog.details)
        .where(AuditLog.action == "plan.created")
        .order_by(AuditLog.id.desc())
    )
    result = {}
    for details in candidates:
        try:
            parsed = json.loads(details)
        except (TypeError, json.JSONDecodeError):
            continue
        plan_id = parsed.get("plan_id")
        if isinstance(plan_id, int):
            result.setdefault(plan_id, parsed)
    return result


def _plan_response(plan: DispatchPlan, details: dict | None = None) -> dict:
    details = details or {}
    return {
        "id": plan.id,
        "post_id": plan.post_id,
        "depot_id": plan.depot_id,
        "item_id": plan.item_id,
        "quantity": plan.quantity,
        "status": plan.status,
        "created_at": plan.created_at.isoformat(),
        "vehicle_id": details.get("vehicle_id"),
        "vehicle_name": details.get("vehicle_name"),
        "primary_route": details.get("primary_route"),
        "backup_route": details.get("backup_route"),
        "departure_at": details.get("departure_at"),
        "explanation": details.get("explanation"),
        "risk": details.get("risk"),
        "requires_commander_approval": details.get("requires_commander_approval", True),
    }


def _derived_alerts(session: Session, now: datetime | None = None) -> list[dict]:
    now = now or datetime.utcnow()
    alerts = []
    weather_events = session.scalars(
        select(WeatherEvent).where(
            func.lower(WeatherEvent.severity) == "severe",
            WeatherEvent.starts_at <= now + timedelta(days=7),
            WeatherEvent.ends_at >= now,
        )
    )
    for event in weather_events:
        alerts.append(
            {
                "id": None,
                "type": "severe_weather",
                "post_id": event.affected_post_id,
                "item_id": None,
                "severity": "critical",
                "message": f"{event.name}: {event.description}",
                "suggestion": "Review affected dispatches and defer travel until the event clears.",
                "created_at": now.isoformat(),
                "resolved": False,
                "source": "weather_events",
            }
        )

    closed_roads = list(
        session.scalars(select(RoadSegment).where(func.lower(RoadSegment.status) == "closed"))
    )
    closed_ids = {road.id for road in closed_roads}
    for road in closed_roads:
        for post_id in {road.start_post_id, road.end_post_id}:
            alerts.append(
                {
                    "id": None,
                    "type": "closed_road",
                    "post_id": post_id,
                    "item_id": None,
                    "severity": "high",
                    "message": f"Road segment {road.name} is closed.",
                    "suggestion": "Recalculate the primary and backup routes before dispatch.",
                    "created_at": now.isoformat(),
                    "resolved": False,
                    "source": "road_segments",
                }
            )

    plan_details = _all_plan_metadata(session)
    for plan in session.scalars(
        select(DispatchPlan).where(
            DispatchPlan.status.in_(("proposed", "approved", "in_transit"))
        )
    ):
        metadata = plan_details.get(plan.id, {})
        primary = metadata.get("primary_route") or {}
        route_segment_ids = {
            segment.get("id")
            for segment in primary.get("segments", [])
            if isinstance(segment, dict) and segment.get("id") is not None
        }
        affected = sorted(route_segment_ids.intersection(closed_ids))
        if affected:
            alerts.append(
                {
                    "id": None,
                    "type": "planned_route_closed",
                    "post_id": plan.post_id,
                    "item_id": plan.item_id,
                    "severity": "critical",
                    "message": f"Plan {plan.id} primary route contains closed segment(s): {affected}.",
                    "suggestion": "Select the stored backup route or recalculate an alternate route.",
                    "created_at": now.isoformat(),
                    "resolved": False,
                    "source": "dispatch_plans",
                    "plan_id": plan.id,
                }
            )

    for post in session.scalars(select(Post).order_by(Post.id)):
        summary = _post_summary(session, post)
        for stock in summary["stocks"]:
            risk = stock["risk"]
            if risk["level"] in {"High", "Critical"}:
                critical = risk["level"] == "Critical"
                alerts.append(
                    {
                        "id": None,
                        "type": (
                            "stock_out_before_safe_window"
                            if critical
                            else "low_stock"
                        ),
                        "post_id": post.id,
                        "item_id": stock["item_id"],
                        "severity": "critical" if critical else "high",
                        "message": (
                            f"{post.name} may exhaust {stock['item']} in "
                            f"{risk['days_stock_left']} days, before its safe window "
                            f"in {risk['days_to_next_safe_window']} days."
                            if critical
                            else (
                                f"{post.name} has a short {stock['item']} buffer: "
                                f"{risk['days_stock_left']} days of stock before a "
                                f"safe window in {risk['days_to_next_safe_window']} days."
                            )
                        ),
                        "suggestion": (
                            "Create and route a replenishment plan immediately."
                            if critical
                            else "Review the demand forecast and pre-position replenishment stock."
                        ),
                        "created_at": now.isoformat(),
                        "resolved": False,
                        "source": "supply_risk",
                    }
                )
    return alerts


class PlanRequest(BaseModel):
    post_id: int
    depot_id: int
    item_id: int
    quantity: float = Field(gt=0)
    vehicle_id: int
    primary_route: dict = Field(min_length=1)
    backup_route: dict | None = Field(...)
    departure_at: datetime
    explanation: str = Field(min_length=1, max_length=2000)


class PlanDecision(BaseModel):
    decision: str
    comment: str = Field(default="", max_length=1000)


class WhatIfRequest(BaseModel):
    post_id: int
    item_id: int
    depot_id: int | None = None
    road_segment_id: int | None = None
    additional_quantity: float = Field(default=0, ge=0)
    demand_multiplier: float = Field(default=1, gt=0, le=10)
    road_closure: bool = False
    road_closure_days: int = Field(default=1, ge=1, le=30)


@app.get("/health")
def health(session: Annotated[Session, Depends(get_db)]):
    database = "ok"
    error = None
    try:
        session.execute(select(1))
    except SQLAlchemyError as exc:
        database = "error"
        error = f"{type(exc).__name__}: {getattr(exc, 'orig', exc)}"
    return {
        "status": "ok" if database == "ok" else "degraded",
        "data": "synthetic prototype",
        "database": database,
        "database_error": error,
        "mqtt": mqtt.status.copy(),
    }


@app.post("/api/auth/token")
def login(
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    session: Annotated[Session, Depends(get_db)],
):
    user = session.scalar(select(User).where(User.username == username))
    if user is None or not user.active or not verify_password(password, user.password_hash):
        _record_audit(session, user.id if user else None, "auth.login_failed", {"username": username})
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    _record_audit(session, user.id, "auth.login", {"role": user.role})
    return {
        "access_token": create_access_token(user),
        "token_type": "bearer",
        "expires_in": int(os.getenv("ACCESS_TOKEN_MINUTES", "60")) * 60,
        "user": {"id": user.id, "username": user.username, "role": user.role},
    }


@app.get("/api/dashboard")
def dashboard(
    session: Annotated[Session, Depends(get_db)],
    user: Planner,
):
    posts = list(session.scalars(select(Post).order_by(Post.id)))
    post_summaries = [_post_summary(session, post) for post in posts]
    persisted_alerts = int(session.scalar(select(func.count(Alert.id)).where(Alert.resolved.is_(False))) or 0)
    risk_alerts = len(_derived_alerts(session))
    active_plans = int(
        session.scalar(
            select(func.count(DispatchPlan.id)).where(
                DispatchPlan.status.in_(("proposed", "approved", "in_transit"))
            )
        )
        or 0
    )
    available_vehicles = int(session.scalar(select(func.count(Vehicle.id)).where(Vehicle.available.is_(True))) or 0)
    return {
        "summary": {
            "posts": len(posts),
            "depots": int(session.scalar(select(func.count(Depot.id))) or 0),
            "active_alerts": persisted_alerts + risk_alerts,
            "active_plans": active_plans,
            "available_vehicles": available_vehicles,
        },
        "posts": post_summaries,
        "weather_events": [
            {
                "id": event.id,
                "name": event.name,
                "severity": event.severity,
                "starts_at": event.starts_at.isoformat(),
                "ends_at": event.ends_at.isoformat(),
                "affected_post_id": event.affected_post_id,
                "description": event.description,
            }
            for event in session.scalars(
                select(WeatherEvent).where(WeatherEvent.ends_at >= datetime.utcnow()).order_by(WeatherEvent.starts_at)
            )
        ],
    }


@app.get("/api/posts")
def list_posts(session: Annotated[Session, Depends(get_db)], user: Planner):
    return [_post_summary(session, post) for post in session.scalars(select(Post).order_by(Post.id))]


@app.get("/api/depots")
def list_depots(session: Annotated[Session, Depends(get_db)], user: Planner):
    return [
        {
            "id": depot.id,
            "name": depot.name,
            "altitude_m": depot.altitude_m,
            "location": _location(depot.location),
        }
        for depot in session.scalars(select(Depot).order_by(Depot.id))
    ]


@app.get("/api/items")
def list_items(session: Annotated[Session, Depends(get_db)], user: Planner):
    return [
        {"id": item.id, "name": item.name, "unit": item.unit}
        for item in session.scalars(select(Item).order_by(Item.id))
    ]


@app.get("/api/vehicles")
def list_vehicles(session: Annotated[Session, Depends(get_db)], user: Planner):
    return [
        {
            "id": vehicle.id,
            "name": vehicle.name,
            "vehicle_type": vehicle.vehicle_type,
            "capacity_kg": vehicle.capacity_kg,
            "available": vehicle.available,
        }
        for vehicle in session.scalars(select(Vehicle).order_by(Vehicle.id))
    ]


@app.get("/api/posts/{id}")
def get_post(id: int, session: Annotated[Session, Depends(get_db)], user: Planner):
    post = session.get(Post, id)
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    return _post_summary(session, post)


@app.get("/api/posts/{id}/forecast")
def forecast_post(
    id: int,
    item_id: int,
    session: Annotated[Session, Depends(get_db)],
    user: Planner,
):
    post = session.get(Post, id)
    if post is None or session.get(Item, item_id) is None:
        raise HTTPException(status_code=404, detail="Post or item not found")
    result = get_forecast(session, id, item_id, altitude_m=post.altitude_m)
    recent = list(
        session.scalars(
            select(ConsumptionHistory)
            .where(
                ConsumptionHistory.post_id == id,
                ConsumptionHistory.item_id == item_id,
                ConsumptionHistory.day >= datetime.utcnow() - timedelta(days=7),
            )
        )
    )
    stock = session.scalar(
        select(StockLevel).where(
            StockLevel.post_id == id,
            StockLevel.item_id == item_id,
        )
    )
    average_daily = (
        sum(row.quantity for row in recent) / len(recent) if recent else None
    )
    days_stock_left = (
        float(stock.quantity) / average_daily
        if stock is not None and average_daily is not None and average_daily > 0
        else None
    )
    safe_days, window_explanation = _safe_window_estimate(session, post)
    result.update({"post_id": id, "item_id": item_id, "horizon_days": 14})
    result["supply_risk"] = supply_risk(
        days_stock_left,
        safe_days,
        window_explanation,
    )
    result["average_daily_demand_7d"] = (
        round(average_daily, 2) if average_daily is not None else None
    )
    return result


@app.post("/api/ml/retrain")
def ml_retrain(session: Annotated[Session, Depends(get_db)], user: Administrator):
    result = retrain_models(session)
    _record_audit(session, user.id, "ml.retrain", {"trained": len(result["trained"]), "skipped": len(result["skipped"])})
    return result


@app.get("/api/routes")
def get_route(
    depot_id: int,
    post_id: int,
    session: Annotated[Session, Depends(get_db)],
    user: Planner,
):
    try:
        return route_between(session, depot_id, post_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/alerts")
def list_alerts(
    session: Annotated[Session, Depends(get_db)],
    user: Planner,
    include_resolved: bool = False,
):
    query = select(Alert).order_by(Alert.created_at.desc())
    if not include_resolved:
        query = query.where(Alert.resolved.is_(False))
    persisted = [
        {
            "id": alert.id,
            "post_id": alert.post_id,
            "item_id": alert.item_id,
            "severity": alert.severity,
            "message": alert.message,
            "suggestion": alert.suggestion,
            "created_at": alert.created_at.isoformat(),
            "resolved": alert.resolved,
        }
        for alert in session.scalars(query)
    ]
    return persisted if include_resolved else persisted + _derived_alerts(session)


@app.post("/api/what-if")
def what_if(
    request: WhatIfRequest,
    session: Annotated[Session, Depends(get_db)],
    user: Planner,
):
    post = session.get(Post, request.post_id)
    if post is None or session.get(Item, request.item_id) is None:
        raise HTTPException(status_code=404, detail="Post or item not found")
    stock = session.scalar(
        select(StockLevel).where(
            StockLevel.post_id == request.post_id,
            StockLevel.item_id == request.item_id,
        )
    )
    history = session.scalars(
        select(ConsumptionHistory)
        .where(
            ConsumptionHistory.post_id == request.post_id,
            ConsumptionHistory.item_id == request.item_id,
        )
        .order_by(ConsumptionHistory.day.desc())
        .limit(30)
    ).all()
    daily_demand = (
        sum(row.quantity for row in history) / len(history) if history else 0.0
    ) * request.demand_multiplier
    current_stock = float(stock.quantity) if stock else 0.0
    days_to_window, window_explanation = _safe_window_estimate(session, post)
    before = supply_risk(
        current_stock / daily_demand if daily_demand > 0 else None,
        days_to_window,
        window_explanation,
    )
    after = supply_risk(
        (current_stock + request.additional_quantity) / daily_demand
        if daily_demand > 0
        else None,
        days_to_window,
        window_explanation,
    )
    route_impact = None
    projected_days_to_window = days_to_window
    if request.road_closure:
        if request.depot_id is None or request.road_segment_id is None:
            raise HTTPException(
                status_code=422,
                detail="depot_id and road_segment_id are required to simulate a road closure",
            )
        segment = session.get(RoadSegment, request.road_segment_id)
        if segment is None:
            raise HTTPException(status_code=404, detail="Road segment not found")
        if segment.status.lower() == "closed":
            raise HTTPException(status_code=409, detail="Road segment is already closed")
        try:
            current_route = route_between(
                session, request.depot_id, request.post_id, use_osmnx=False
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=f"Baseline route unavailable: {exc}")
        try:
            alternate_route = route_between(
                session,
                request.depot_id,
                request.post_id,
                closed_segment_id=request.road_segment_id,
                use_osmnx=False,
            )
            route_impact = {
                "available": True,
                "closed_segment_id": segment.id,
                "baseline_distance_km": current_route["distance_km"],
                "alternate_distance_km": alternate_route["distance_km"],
                "distance_change_km": round(
                    alternate_route["distance_km"] - current_route["distance_km"], 2
                ),
                "alternate_risk_score": alternate_route["risk_score"],
            }
        except ValueError:
            route_impact = {
                "available": False,
                "closed_segment_id": segment.id,
                "baseline_distance_km": current_route["distance_km"],
                "alternate_distance_km": None,
                "distance_change_km": None,
                "alternate_risk_score": None,
            }
        if not route_impact["available"]:
            projected_days_to_window = days_to_window + request.road_closure_days
            after = supply_risk(
                (current_stock + request.additional_quantity) / daily_demand
                if daily_demand > 0
                else None,
                projected_days_to_window,
                (
                    f"The selected road is simulated closed for "
                    f"{request.road_closure_days} days and no alternate open route exists."
                ),
            )
    _record_audit(
        session,
        user.id,
        "what_if.run",
        {
            "post_id": request.post_id,
            "item_id": request.item_id,
            "additional_quantity": request.additional_quantity,
            "demand_multiplier": request.demand_multiplier,
            "road_closure": request.road_closure,
            "road_closure_days": request.road_closure_days,
            "depot_id": request.depot_id,
            "road_segment_id": request.road_segment_id,
        },
    )
    return {
        "post_id": request.post_id,
        "item_id": request.item_id,
        "current_stock": current_stock,
        "projected_stock": current_stock + request.additional_quantity,
        "daily_demand": round(daily_demand, 2),
        "current_risk": before,
        "projected_risk": after,
        "road_closure_days": request.road_closure_days if request.road_closure else 0,
        "projected_days_to_next_safe_window": round(projected_days_to_window, 1),
        "risk_change_days": (
            round(after["risk_days"] - before["risk_days"], 1)
            if after["risk_days"] is not None and before["risk_days"] is not None
            else None
        ),
        "route_impact": route_impact,
        "road_closure_impact": (
            (
                "No alternate open route exists."
                if route_impact is not None and not route_impact["available"]
                else "Alternate route recalculated with the selected segment closed."
            )
            if request.road_closure
            else "No additional route closure applied."
        ),
    }


@app.get("/api/plans")
def list_plans(
    session: Annotated[Session, Depends(get_db)],
    user: Planner,
    status: str | None = None,
):
    query = select(DispatchPlan).order_by(DispatchPlan.created_at.desc())
    if status:
        query = query.where(DispatchPlan.status == status)
    metadata = _all_plan_metadata(session)
    return [
        _plan_response(plan, metadata.get(plan.id))
        for plan in session.scalars(query)
    ]


@app.post("/api/plans", status_code=201)
def create_plan(
    request: PlanRequest,
    session: Annotated[Session, Depends(get_db)],
    user: Planner,
):
    post = session.get(Post, request.post_id)
    vehicle = session.get(Vehicle, request.vehicle_id)
    if post is None or session.get(Depot, request.depot_id) is None or session.get(Item, request.item_id) is None:
        raise HTTPException(status_code=404, detail="Post, depot, or item not found")
    if vehicle is None:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    if not vehicle.available:
        raise HTTPException(status_code=409, detail="Vehicle is not available")
    try:
        calculated_routes = route_between(
            session, request.depot_id, request.post_id, use_osmnx=False
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=f"Cannot plan without a primary route: {exc}")

    def segment_ids(route):
        if route is None:
            return None
        if not isinstance(route, dict):
            raise HTTPException(status_code=422, detail="Routes must be route objects returned by /api/routes")
        segments = route.get("segments")
        if not isinstance(segments, list):
            raise HTTPException(status_code=422, detail="Route object must include a segments array")
        return [segment.get("id") for segment in segments if isinstance(segment, dict)]

    primary_route = calculated_routes["primary_route"]
    backup_route = calculated_routes["backup_route"]
    if segment_ids(request.primary_route) != segment_ids(primary_route):
        raise HTTPException(status_code=409, detail="Primary route has changed; fetch /api/routes again")
    if segment_ids(request.backup_route) != segment_ids(backup_route):
        raise HTTPException(status_code=409, detail="Backup route has changed; fetch /api/routes again")

    recent_demand = session.scalar(
        select(func.avg(ConsumptionHistory.quantity)).where(
            ConsumptionHistory.post_id == post.id,
            ConsumptionHistory.item_id == request.item_id,
            ConsumptionHistory.day >= datetime.utcnow() - timedelta(days=7),
        )
    )
    stock = session.scalar(
        select(StockLevel).where(
            StockLevel.post_id == post.id,
            StockLevel.item_id == request.item_id,
        )
    )
    days_stock_left = (
        float(stock.quantity) / float(recent_demand)
        if stock is not None and recent_demand and recent_demand > 0
        else None
    )
    safe_days, safe_explanation = _safe_window_estimate(session, post)
    risk = supply_risk(days_stock_left, safe_days, safe_explanation)
    large_quantity = request.quantity >= float(os.getenv("LARGE_PLAN_QUANTITY", "250"))
    requires_approval = (
        risk["level"] == "Critical"
        or risk["risk_days"] is None
        or large_quantity
    )
    plan = DispatchPlan(
        post_id=request.post_id,
        depot_id=request.depot_id,
        item_id=request.item_id,
        quantity=request.quantity,
        status="proposed" if requires_approval else "approved",
    )
    session.add(plan)
    session.flush()
    metadata = {
        "plan_id": plan.id,
        "vehicle_id": vehicle.id,
        "vehicle_name": vehicle.name,
        "primary_route": primary_route,
        "backup_route": backup_route,
        "departure_at": request.departure_at.isoformat(),
        "explanation": request.explanation,
        "risk": risk,
        "requires_commander_approval": requires_approval,
        "large_quantity_threshold": float(os.getenv("LARGE_PLAN_QUANTITY", "250")),
        "request": {
            "post_id": request.post_id,
            "depot_id": request.depot_id,
            "item_id": request.item_id,
            "quantity": request.quantity,
        },
    }
    _record_audit(session, user.id, "plan.created", metadata)
    return _plan_response(plan, metadata)


@app.post("/api/plans/{id}/decision")
def decide_plan(
    id: int,
    request: PlanDecision,
    session: Annotated[Session, Depends(get_db)],
    user: Commander,
):
    decision = request.decision.strip().lower()
    if decision not in {"approved", "rejected"}:
        raise HTTPException(status_code=422, detail="decision must be 'approved' or 'rejected'")
    plan = session.get(DispatchPlan, id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    if plan.status != "proposed":
        raise HTTPException(status_code=409, detail=f"Plan is already {plan.status}")
    plan.status = decision
    session.flush()
    _record_audit(
        session,
        user.id,
        "plan.decision",
        {"plan_id": plan.id, "decision": decision, "comment": request.comment},
    )
    return {"id": plan.id, "status": plan.status, "decision_by": user.username}


@app.get("/api/audit")
def list_audit(
    session: Annotated[Session, Depends(get_db)],
    user: Administrator,
    limit: int = Query(default=100, ge=1, le=500),
):
    rows = session.execute(
        select(AuditLog, User.username)
        .outerjoin(User, AuditLog.user_id == User.id)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    ).all()
    return [
        {
            "id": log.id,
            "user_id": log.user_id,
            "username": username,
            "action": log.action,
            "details": log.details,
            "created_at": log.created_at.isoformat(),
        }
        for log, username in rows
    ]
