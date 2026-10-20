"""Supply-window risk from stock coverage and upcoming operational disruptions."""

from __future__ import annotations


def supply_risk(
    days_stock_left: float | None,
    days_to_next_safe_window: float | None,
    safe_window_explanation: str,
) -> dict:
    """Compare stock coverage with the next safe resupply window.

    A positive risk_days value means stock is projected to run out before the
    next safe window; negative values are the remaining stock buffer.
    """
    if days_stock_left is None:
        return {
            "days_stock_left": None,
            "coverage_days": None,
            "days_to_next_safe_window": days_to_next_safe_window,
            "risk_days": None,
            "level": "Low",
            "score": 0.0,
            "explanation": (
                "Demand history is unavailable, so stock coverage cannot be "
                f"estimated. {safe_window_explanation}"
            ),
        }
    safe_days = days_to_next_safe_window if days_to_next_safe_window is not None else 14.0
    risk_days = round(float(safe_days) - float(days_stock_left), 1)
    if risk_days >= 0:
        level = "Critical"
        implication = "Expected stock depletion is on or before the next safe resupply window."
    elif risk_days > -2:
        level = "High"
        implication = "Stock provides less than two days of buffer beyond the next safe window."
    elif risk_days > -7:
        level = "Medium"
        implication = "Stock covers the next safe window, with a limited buffer."
    else:
        level = "Low"
        implication = "Stock is expected to cover the next safe window with more than seven days of buffer."
    score = risk_score(risk_days, level)
    return {
        "days_stock_left": round(float(days_stock_left), 1),
        "coverage_days": round(float(days_stock_left), 1),
        "days_to_next_safe_window": round(float(safe_days), 1),
        "risk_days": risk_days,
        "level": level,
        "score": score,
        "explanation": (
            f"Risk days = {safe_days:.1f} days to the next safe window - "
            f"{days_stock_left:.1f} days of stock = {risk_days:.1f} days. "
            f"{implication} {safe_window_explanation}"
        ),
    }


def risk_score(risk_days: float | None, level: str | None = None) -> float:
    """Convert days-to-window delta into a frontend-compatible 0–100 score."""
    if risk_days is None:
        return 0.0
    if level is None:
        if risk_days >= 0:
            level = "Critical"
        elif risk_days > -2:
            level = "High"
        elif risk_days > -7:
            level = "Medium"
        else:
            level = "Low"
    if level == "Critical":
        score = 90 + max(0.0, risk_days) * 2
    elif level == "High":
        score = 80 + (risk_days + 2) * 5
    elif level == "Medium":
        score = 60 + (risk_days + 7) * 2
    else:
        score = 40 + risk_days
    return round(max(0.0, min(100.0, score)), 1)


def post_risk_score(item_risks: list[dict]) -> float:
    """Use the highest item risk for map markers and aggregate post risk."""
    return max((float(risk.get("score", 0)) for risk in item_risks), default=0.0)


def route_risk(closure_risk: float, status: str, altitude_m: int = 0) -> float:
    """Route score for display; routing edge cost uses the required closure-risk formula."""
    closure_component = 100.0 if status.lower() == "closed" else max(0.0, min(100.0, closure_risk * 100))
    altitude_component = min(30.0, max(0.0, (altitude_m - 2500) / 50))
    return round(min(100.0, closure_component * 0.8 + altitude_component * 0.2), 1)
