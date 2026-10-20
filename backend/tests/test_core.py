"""Focused tests for forecast, risk, routing, auth, and API contracts."""

import builtins
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest

from app.forecasting import _forecast_series, mean_absolute_percentage_error
from app.main import WhatIfRequest, app
from app.mqtt import _post_id_from_topic
from app.models import User
from app.risk import post_risk_score, route_risk, supply_risk
from app.routing import _geospatial_road_rows, _weighted_length, route_between
from app.security import SECRET_KEY, create_access_token, hash_password, verify_password


def test_risk_days_compare_safe_window_with_stock_days():
    critical = supply_risk(4, 6, "Severe weather delays access.")
    high = supply_risk(7, 6, "A road closure delays access.")
    low = supply_risk(20, 12, "No disruption is expected.")
    assert critical["risk_days"] == 2
    assert critical["level"] == "Critical"
    assert critical["coverage_days"] == 4
    assert isinstance(critical["score"], float)
    assert critical["score"] > high["score"]
    assert high["risk_days"] == -1
    assert high["level"] == "High"
    assert supply_risk(10, 6, "Limited buffer.")["level"] == "Medium"
    assert low["level"] == "Low"
    assert "Severe weather" in critical["explanation"]


def test_unknown_demand_is_explicit():
    result = supply_risk(None, 10, "Weather clears in ten days.")
    assert result["level"] == "Low"
    assert result["risk_days"] is None
    assert "cannot be estimated" in result["explanation"]


def test_closed_route_is_high_risk():
    assert route_risk(0.1, "closed") == 80.0


def test_route_cost_scales_with_closure_risk():
    assert _weighted_length(10, 0.2) == 12
    assert _weighted_length(10, -0.2) == 10


def test_post_uses_worst_item_numeric_risk_for_frontend_marker():
    risks = [
        supply_risk(20, 12, "No disruptions."),
        supply_risk(4, 6, "Storm."),
    ]
    assert post_risk_score(risks) == risks[1]["score"]


def test_mape_ignores_zero_actuals():
    assert mean_absolute_percentage_error([0, 10], [100, 8]) == 20.0
    assert mean_absolute_percentage_error([0], [1]) is None


def test_forecast_returns_exactly_fourteen_days():
    now = datetime(2026, 1, 1)
    history = [
        SimpleNamespace(day=now + timedelta(days=index), quantity=10.0, temperature_c=2.0)
        for index in range(40)
    ]
    result = _forecast_series(history, 14, use_xgboost=False)
    assert len(result["forecast"]) == 14
    assert result["model"] == "seasonal_baseline"
    assert "mape" in result


def test_missing_ml_packages_fall_back_without_import_failure(monkeypatch):
    original_import = builtins.__import__

    def import_without_ml(name, *args, **kwargs):
        if name.startswith(("xgboost", "prophet")) or name == "pandas":
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)

    now = datetime(2026, 1, 1)
    history = [
        SimpleNamespace(day=now + timedelta(days=index), quantity=10.0, temperature_c=2.0)
        for index in range(20)
    ]
    monkeypatch.setattr(builtins, "__import__", import_without_ml)
    result = _forecast_series(history, 14, use_xgboost=True)
    assert result["model"] == "seasonal_baseline"
    assert len(result["forecast"]) == 14
    assert result["prophet_baseline"] is None


def test_xgboost_feature_vector_contains_requested_covariates(monkeypatch):
    captured_fit = {}
    captured_predict = {}

    class FakeXGB:
        def __init__(self, **kwargs):
            pass

        def fit(self, features, labels):
            captured_fit["features"] = features

        def predict(self, features):
            captured_predict["features"] = features
            return [10.0] * len(features)

    now = datetime(2026, 1, 1)
    history = [
        SimpleNamespace(day=now + timedelta(days=index), quantity=10 + index, temperature_c=2.0 + index)
        for index in range(40)
    ]
    monkeypatch.setattr("app.forecasting._optional_xgboost", lambda: FakeXGB)
    monkeypatch.setattr(
        "app.forecasting._prophet_baseline",
        lambda rows, horizon: (None, "not installed"),
    )
    result = _forecast_series(history, 14, use_xgboost=True, altitude_m=2700)
    features = captured_fit["features"]
    assert features.shape[1] == 8
    assert features[7, 4] == history[7].day.month
    assert features[7, 5] == 2700
    assert features[7, 6] == history[7].temperature_c
    assert features[7, 7] == sum(row.quantity for row in history[:7]) / 7
    assert captured_predict["features"].shape[1] == 8
    assert len(result["forecast"]) == 14


def test_required_api_routes_are_registered():
    routes = {route.path for route in app.routes}
    assert {
        "/api/auth/token",
        "/api/dashboard",
        "/api/depots",
        "/api/items",
        "/api/vehicles",
        "/api/posts",
        "/api/posts/{id}",
        "/api/posts/{id}/forecast",
        "/api/ml/retrain",
        "/api/routes",
        "/api/alerts",
        "/api/what-if",
        "/api/plans",
        "/api/plans/{id}/decision",
        "/api/audit",
    }.issubset(routes)


def test_what_if_closure_duration_is_bounded():
    request = WhatIfRequest(post_id=1, item_id=1, road_closure=True, road_closure_days=7)
    assert request.road_closure_days == 7
    with pytest.raises(ValueError):
        WhatIfRequest(post_id=1, item_id=1, road_closure_days=0)


def test_password_hash_and_token():
    password_hash = hash_password("fake-demo-password")
    assert verify_password("fake-demo-password", password_hash)
    assert not verify_password("incorrect", password_hash)
    token = create_access_token(User(id=7, username="demo", role="planner"))
    assert jwt.decode(token, SECRET_KEY, algorithms=["HS256"])["sub"] == "7"


def test_plan_schema_requires_dispatch_details():
    schema = app.openapi()["components"]["schemas"]["PlanRequest"]
    assert {
        "post_id",
        "depot_id",
        "item_id",
        "quantity",
        "vehicle_id",
        "primary_route",
        "backup_route",
        "departure_at",
        "explanation",
    }.issubset(schema["required"])


def test_mqtt_topic_binds_updates_to_post():
    assert _post_id_from_topic("purva/stock/12") == 12
    with pytest.raises(ValueError):
        _post_id_from_topic("purva/stock/update")


def test_successful_osmnx_routes_are_returned_as_primary_and_backup(monkeypatch):
    primary = {"distance_km": 10, "segments": []}
    backup = {"distance_km": 12, "segments": []}
    depot = SimpleNamespace(id=1, location=None)
    post = SimpleNamespace(id=2, location=None)

    class Session:
        def get(self, model, identifier):
            return depot if identifier == 1 else post

    monkeypatch.setattr(
        "app.routing._attempt_osmnx_route",
        lambda depot, post: {
            "used": True,
            "source": "openstreetmap",
            "primary_route": primary,
            "backup_route": backup,
        },
    )
    result = route_between(Session(), 1, 2)
    assert result["source"] == "openstreetmap"
    assert result["primary_route"] == primary
    assert result["backup_route"] == backup


def test_geopandas_is_used_for_synthetic_graph_data(monkeypatch):
    calls = {}

    class FakeGeoDataFrame:
        def __init__(self, rows, **kwargs):
            calls["rows"] = rows
            calls["kwargs"] = kwargs

        def iterrows(self):
            return iter(())

    fake_geopandas = SimpleNamespace(GeoDataFrame=FakeGeoDataFrame)
    monkeypatch.setitem(sys.modules, "geopandas", fake_geopandas)
    rows, source = _geospatial_road_rows([])
    assert rows == []
    assert source == "synthetic_geopandas_networkx"
    assert calls["kwargs"]["crs"] == "EPSG:4326"
