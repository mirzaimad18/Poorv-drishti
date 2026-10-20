"""Demand forecasts with optional XGBoost and Prophet implementations."""

from __future__ import annotations

import math
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ConsumptionHistory, Post

HORIZON_DAYS = 14
ARTIFACT_DIR = Path(
    os.getenv("MODEL_DIR", str(Path(__file__).resolve().parents[1] / "model_artifacts"))
)


def _features(
    day: date,
    temperature: float,
    altitude_m: float,
    avg_7day_quantity: float,
) -> list[float]:
    angle = math.tau * day.timetuple().tm_yday / 365.25
    return [
        float(day.toordinal()),
        math.sin(angle),
        math.cos(angle),
        float(day.weekday()),
        float(day.month),
        float(altitude_m),
        float(temperature),
        float(avg_7day_quantity),
    ]


def _feature_rows(history, altitude_m: float) -> list[list[float]]:
    rows = []
    previous_quantities = []
    for row in history:
        recent_average = (
            sum(previous_quantities[-7:]) / min(7, len(previous_quantities))
            if previous_quantities
            else float(row.quantity)
        )
        rows.append(
            _features(
                row.day.date(),
                row.temperature_c,
                altitude_m,
                recent_average,
            )
        )
        previous_quantities.append(float(row.quantity))
    return rows


def mean_absolute_percentage_error(actual: list[float], predicted: list[float]) -> float | None:
    pairs = [(a, p) for a, p in zip(actual, predicted) if a != 0]
    if not pairs:
        return None
    return round(sum(abs(a - p) / abs(a) for a, p in pairs) / len(pairs) * 100, 2)


def _get_series(session: Session, post_id: int, item_id: int):
    return list(
        session.scalars(
            select(ConsumptionHistory)
            .where(
                ConsumptionHistory.post_id == post_id,
                ConsumptionHistory.item_id == item_id,
            )
            .order_by(ConsumptionHistory.day)
        )
    )


def _optional_xgboost():
    try:
        from xgboost import XGBRegressor
    except (ImportError, ModuleNotFoundError):
        return None
    return XGBRegressor


def _prophet_baseline(history, horizon: int) -> tuple[list[float] | None, str | None]:
    if len(history) < 2:
        return None, "Prophet needs at least two observations"
    try:
        import pandas as pd
        from prophet import Prophet
    except (ImportError, ModuleNotFoundError):
        return None, "Prophet dependencies are not installed"

    frame = pd.DataFrame(
        [{"ds": row.day, "y": row.quantity} for row in history]
    )
    model = Prophet(daily_seasonality=False, weekly_seasonality=True, yearly_seasonality=True)
    model.fit(frame)
    future = model.make_future_dataframe(periods=horizon, freq="D")
    forecast = model.predict(future).tail(horizon)["yhat"].tolist()
    return [max(0.0, float(value)) for value in forecast], None


def _seasonal_predictions(history, horizon: int) -> list[float]:
    recent = [float(row.quantity) for row in history[-min(28, len(history)):]]
    average = sum(recent) / len(recent) if recent else 0.0
    if len(history) >= 365:
        by_doy = {}
        for row in history[-730:]:
            by_doy.setdefault(row.day.timetuple().tm_yday, []).append(float(row.quantity))
        start = history[-1].day.date()
        return [
            max(
                0.0,
                sum(by_doy.get((start + timedelta(days=offset)).timetuple().tm_yday, [average]))
                / len(by_doy.get((start + timedelta(days=offset)).timetuple().tm_yday, [average])),
            )
            for offset in range(1, horizon + 1)
        ]
    return [max(0.0, average) for _ in range(horizon)]


def _forecast_series(
    history,
    horizon: int,
    use_xgboost: bool,
    saved_model=None,
    altitude_m: float = 0.0,
) -> dict[str, Any]:
    if not history:
        return {
            "forecast": [],
            "model": "unavailable",
            "mape": None,
            "prophet_baseline": None,
            "prophet_mape": None,
            "history_points": 0,
        }

    # Reserve the most recent 14 points for a genuine out-of-sample score.
    split = max(1, len(history) - min(horizon, max(1, len(history) // 5)))
    train, test = history[:split], history[split:]
    XGBRegressor = _optional_xgboost() if use_xgboost else None
    model_name = "seasonal_baseline"
    model = None
    if XGBRegressor and len(train) >= 14:
        import numpy as np

        model = XGBRegressor(
            n_estimators=80,
            max_depth=3,
            learning_rate=0.05,
            objective="reg:squarederror",
            n_jobs=1,
            random_state=17,
        )
        model.fit(
            np.asarray(_feature_rows(train, altitude_m)),
            np.asarray([row.quantity for row in train]),
        )
        model_name = "xgboost"

    if model is not None and test:
        import numpy as np

        validation = model.predict(
            np.asarray(_feature_rows(history, altitude_m)[split:])
        )
        mape = mean_absolute_percentage_error(
            [float(row.quantity) for row in test], [float(value) for value in validation]
        )
    else:
        seasonal_test = _seasonal_predictions(train, len(test)) if test else []
        mape = mean_absolute_percentage_error(
            [float(row.quantity) for row in test], seasonal_test
        )

    latest = history[-1]
    last_day = latest.day.date()
    future_days = [last_day + timedelta(days=offset) for offset in range(1, horizon + 1)]
    inference_model = saved_model or model
    if inference_model is not None:
        import numpy as np

        temperature = sum(row.temperature_c for row in history[-30:]) / min(30, len(history))
        trailing = [float(row.quantity) for row in history[-7:]]
        predictions = []
        for day in future_days:
            recent_average = sum(trailing) / len(trailing) if trailing else 0.0
            value = float(
                inference_model.predict(
                    np.asarray(
                        [_features(day, temperature, altitude_m, recent_average)]
                    )
                )[0]
            )
            value = max(0.0, value)
            predictions.append(value)
            trailing.append(value)
            trailing = trailing[-7:]
    else:
        predictions = _seasonal_predictions(history, horizon)

    prophet_predictions, prophet_error = _prophet_baseline(history, horizon)
    prophet_mape = None
    if prophet_predictions is not None and test:
        prophet_validation, _ = _prophet_baseline(train, len(test))
        if prophet_validation is not None:
            prophet_mape = mean_absolute_percentage_error(
                [float(row.quantity) for row in test], prophet_validation
            )
    return {
        "forecast": [
            {"date": day.isoformat(), "quantity": round(value, 2)}
            for day, value in zip(future_days, predictions)
        ],
        "model": "xgboost" if saved_model is not None else model_name,
        "saved_model": saved_model is not None,
        "mape": mape,
        "prophet_baseline": (
            [
                {"date": day.isoformat(), "quantity": round(value, 2)}
                for day, value in zip(future_days, prophet_predictions)
            ]
            if prophet_predictions is not None
            else None
        ),
        "prophet_mape": prophet_mape,
        "prophet_unavailable_reason": prophet_error,
        "history_points": len(history),
    }


def get_forecast(
    session: Session, post_id: int, item_id: int, altitude_m: float = 0.0
) -> dict[str, Any]:
    history = _get_series(session, post_id, item_id)
    if not history:
        return _forecast_series([], HORIZON_DAYS, use_xgboost=True, altitude_m=altitude_m)
    saved_model = None
    XGBRegressor = _optional_xgboost()
    artifact = ARTIFACT_DIR / f"post-{post_id}-item-{item_id}.json"
    if XGBRegressor and artifact.is_file():
        saved_model = XGBRegressor()
        saved_model.load_model(str(artifact))
    return _forecast_series(
        history,
        HORIZON_DAYS,
        use_xgboost=True,
        saved_model=saved_model,
        altitude_m=altitude_m,
    )


def retrain_models(session: Session) -> dict[str, Any]:
    """Train and persist available XGBoost models for every observed series."""
    pairs = session.execute(
        select(ConsumptionHistory.post_id, ConsumptionHistory.item_id).distinct()
    ).all()
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    trained = []
    skipped = []
    XGBRegressor = _optional_xgboost()
    for post_id, item_id in pairs:
        history = _get_series(session, post_id, item_id)
        post = session.get(Post, post_id)
        altitude_m = float(post.altitude_m) if post else 0.0
        if XGBRegressor is None or len(history) < 14:
            skipped.append({"post_id": post_id, "item_id": item_id, "reason": (
                "XGBoost is not installed" if XGBRegressor is None else "Insufficient history"
            )})
            continue
        import numpy as np

        model = XGBRegressor(
            n_estimators=80,
            max_depth=3,
            learning_rate=0.05,
            objective="reg:squarederror",
            n_jobs=1,
            random_state=17,
        )
        model.fit(
            np.asarray(_feature_rows(history, altitude_m)),
            np.asarray([row.quantity for row in history]),
        )
        model.save_model(str(ARTIFACT_DIR / f"post-{post_id}-item-{item_id}.json"))
        trained.append({"post_id": post_id, "item_id": item_id, "history_points": len(history)})
    return {"trained": trained, "skipped": skipped, "artifact_dir": str(ARTIFACT_DIR)}
