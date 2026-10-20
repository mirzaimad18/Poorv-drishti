"""NetworkX routing over PostGIS road segments with optional OSMnx fallback."""

from __future__ import annotations

import math
import os
from itertools import islice
from typing import Any

import networkx as nx
from geoalchemy2.shape import to_shape
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Depot, Post, RoadSegment
from .risk import route_risk


def _coordinates(geometry) -> tuple[float, float]:
    point = to_shape(geometry)
    return float(point.x), float(point.y)


def _distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def _weighted_length(length_km: float, closure_risk: float) -> float:
    return float(length_km) * (1 + max(0.0, float(closure_risk)))


def _geospatial_road_rows(segments):
    """Materialize road geometries with GeoPandas when available."""
    try:
        import geopandas as gpd
    except (ImportError, ModuleNotFoundError):
        return (
            [
                {"segment": segment, "geometry": to_shape(segment.geometry)}
                for segment in segments
            ],
            "synthetic_networkx",
        )
    roads = gpd.GeoDataFrame(
        [
            {
                "segment": segment,
                "segment_id": segment.id,
                "status": segment.status,
                "closure_risk": segment.closure_risk,
                "geometry": to_shape(segment.geometry),
            }
            for segment in segments
        ],
        columns=["segment", "segment_id", "status", "closure_risk", "geometry"],
        geometry="geometry",
        crs="EPSG:4326",
    )
    return (
        [
            {"segment": row["segment"], "geometry": row.geometry}
            for _, row in roads.iterrows()
        ],
        "synthetic_geopandas_networkx",
    )


def _attempt_osmnx_route(depot: Depot, post: Post) -> dict[str, Any]:
    """Build primary and backup OSM routes when explicitly enabled."""
    try:
        import osmnx as ox
    except (ImportError, ModuleNotFoundError):
        return {"used": False, "reason": "OSMnx is not installed"}
    if os.getenv("OSMNX_ENABLED", "false").lower() != "true":
        return {"used": False, "reason": "OSMnx network access is disabled"}
    try:
        start, end = _coordinates(depot.location), _coordinates(post.location)
        midpoint = ((start[1] + end[1]) / 2, (start[0] + end[0]) / 2)
        graph = ox.graph_from_point(midpoint, dist=max(5000, int(_distance_km(start, end) * 700)), network_type="drive")
        origin = ox.distance.nearest_nodes(graph, X=start[0], Y=start[1])
        destination = ox.distance.nearest_nodes(graph, X=end[0], Y=end[1])
        directed = nx.DiGraph()
        directed.add_nodes_from(graph.nodes(data=True))
        for left, right, _, data in graph.edges(keys=True, data=True):
            if data.get("closed") or str(data.get("status", "")).lower() == "closed":
                continue
            length_m = float(data.get("length", 0.0))
            closure_risk = max(0.0, float(data.get("closure_risk", 0.0)))
            weight = _weighted_length(length_m, closure_risk)
            current = directed.get_edge_data(left, right)
            if current is None or weight < current["weight"]:
                directed.add_edge(
                    left,
                    right,
                    weight=weight,
                    distance_m=length_m,
                    closure_risk=closure_risk,
                    name=data.get("name"),
                )
        paths = list(
            islice(
                nx.shortest_simple_paths(directed, origin, destination, weight="weight"),
                2,
            )
        )

        def describe_osm_path(path):
            edges = [directed.edges[left, right] for left, right in zip(path, path[1:])]
            distance_km = sum(edge["distance_m"] for edge in edges) / 1000
            risk = round(
                sum(edge["closure_risk"] * edge["distance_m"] for edge in edges)
                / max(sum(edge["distance_m"] for edge in edges), 0.01)
                * 100,
                1,
            )
            return {
                "distance_km": round(distance_km, 2),
                "estimated_hours": round(distance_km / 30, 2),
                "risk_score": risk,
                "coordinates": [
                    [graph.nodes[node]["x"], graph.nodes[node]["y"]] for node in path
                ],
                "path": [
                    [graph.nodes[node]["x"], graph.nodes[node]["y"]] for node in path
                ],
                "segments": [
                    {
                        "id": None,
                        "name": str(edge["name"] or "OpenStreetMap road"),
                        "distance_km": round(edge["distance_m"] / 1000, 2),
                        "closure_risk": edge["closure_risk"],
                        "risk_score": round(edge["closure_risk"] * 100, 1),
                    }
                    for edge in edges
                ],
            }

        primary = describe_osm_path(paths[0])
        backup = describe_osm_path(paths[1]) if len(paths) > 1 else None
        return {
            "used": True,
            "source": "openstreetmap",
            "primary_route": primary,
            "backup_route": backup,
        }
    except Exception as exc:
        return {"used": False, "reason": f"OSMnx route unavailable: {type(exc).__name__}: {exc}"}


def route_between(
    session: Session,
    depot_id: int,
    post_id: int,
    closed_segment_id: int | None = None,
    use_osmnx: bool = True,
) -> dict[str, Any]:
    depot = session.get(Depot, depot_id)
    post = session.get(Post, post_id)
    if depot is None or post is None:
        raise ValueError("Unknown depot_id or post_id")

    osm = (
        _attempt_osmnx_route(depot, post)
        if use_osmnx and closed_segment_id is None
        else {"used": False, "reason": "OSMnx skipped for synthetic route calculation"}
    )
    if osm["used"]:
        primary = osm["primary_route"]
        return {
            "depot_id": depot_id,
            "post_id": post_id,
            "source": "openstreetmap",
            "primary_route": primary,
            "backup_route": osm["backup_route"],
            **primary,
            "osm_attempt": {"used": True},
        }

    posts = list(session.scalars(select(Post)))
    graph = nx.Graph()
    positions = {entry.id: _coordinates(entry.location) for entry in posts}
    for entry in posts:
        graph.add_node(entry.id)
    segments = [
        segment
        for segment in session.scalars(select(RoadSegment)).all()
        if segment.status.lower() != "closed" and segment.id != closed_segment_id
    ]
    road_rows, graph_source = _geospatial_road_rows(segments)

    for road_row in road_rows:
        segment = road_row["segment"]
        if segment.status.lower() == "closed" or segment.id == closed_segment_id:
            continue
        if segment.start_post_id not in positions or segment.end_post_id not in positions:
            continue
        line = road_row["geometry"]
        coordinates = list(line.coords)
        distance = sum(
            _distance_km(left, right)
            for left, right in zip(coordinates, coordinates[1:])
        )
        if distance <= 0:
            distance = _distance_km(
                positions[segment.start_post_id], positions[segment.end_post_id]
            )
        risk = route_risk(segment.closure_risk, segment.status)
        closure_risk = max(0.0, float(segment.closure_risk))
        graph.add_edge(
            segment.start_post_id,
            segment.end_post_id,
            weight=_weighted_length(distance, closure_risk),
            distance=distance,
            risk=risk,
            closure_risk=closure_risk,
            segment_id=segment.id,
            name=segment.name,
        )

    depot_point = _coordinates(depot.location)
    nearest = min(posts, key=lambda entry: _distance_km(depot_point, positions[entry.id]))
    access_distance = _distance_km(depot_point, positions[nearest.id])
    access_node = ("depot", depot.id)
    graph.add_node(access_node)
    graph.add_edge(
        access_node,
        nearest.id,
        weight=access_distance,
        distance=access_distance,
        risk=0.0,
        closure_risk=0.0,
        segment_id=None,
        name="synthetic depot access",
    )
    try:
        paths = list(
            islice(nx.shortest_simple_paths(graph, access_node, post.id, weight="weight"), 2)
        )
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        raise ValueError("No open route exists between the selected depot and post")

    def describe_route(path):
        edges = [graph.edges[left, right] for left, right in zip(path, path[1:])]
        distance = sum(edge["distance"] for edge in edges)
        risk_score = round(
            sum(edge["risk"] * edge["distance"] for edge in edges) / max(distance, 0.01), 1
        )
        path_coordinates = [list(depot_point)]
        path_coordinates.extend(list(positions[node]) for node in path[1:] if node in positions)
        return {
            "distance_km": round(distance, 2),
            "estimated_hours": round(distance / 25 * (1 + risk_score / 100), 2),
            "risk_score": risk_score,
            "path": path_coordinates,
            "segments": [
                {
                    "id": edge["segment_id"],
                    "name": edge["name"],
                    "distance_km": round(edge["distance"], 2),
                    "closure_risk": edge["closure_risk"],
                    "risk_score": edge["risk"],
                }
                for edge in edges
            ],
        }

    primary = describe_route(paths[0])
    backup = describe_route(paths[1]) if len(paths) > 1 else None
    return {
        "depot_id": depot_id,
        "post_id": post_id,
        "source": graph_source,
        "primary_route": primary,
        "backup_route": backup,
        # Preserve these fields for existing API consumers.
        **primary,
        "osm_attempt": osm,
    }
