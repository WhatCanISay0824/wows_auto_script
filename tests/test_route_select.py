"""航线选择单元测试（详细设计 4.4 / 4.6）。"""

from __future__ import annotations

import random

import pytest
from src.common.errors import RouteUnavailable
from src.config.models import MapPack, Route
from src.navigation.route import (
    RouteChoice,
    eligible_routes,
    select_route,
    select_route_for_map,
)

WAYS = ((0.0, 0.0), (0.0, -1000.0), (500.0, -2000.0))


def route(route_id: str, spawn_id: str, weight: float = 1.0) -> Route:
    return Route(route_id=route_id, spawn_id=spawn_id, waypoints=WAYS, weight=weight)


ROUTES = (
    route("east_a", "east_1", 1.0),
    route("east_b", "east_1", 0.6),
    route("west_a", "west_1", 1.0),
    route("west_zero", "west_1", 0.0),
    route("east_negative", "east_1", -2.0),
)


def do_select(routes, spawn_id: str, seed: int) -> Route:
    return select_route(routes, spawn_id, seed=seed)


def test_selects_only_routes_bound_to_spawn() -> None:
    for seed in range(20):
        assert do_select(ROUTES, "east_1", seed).spawn_id == "east_1"
        assert do_select(ROUTES, "west_1", seed).route_id == "west_a"


def test_eligible_routes_filters_non_positive_weight() -> None:
    eligible = eligible_routes(ROUTES, "east_1")
    assert [route_.route_id for route_ in eligible] == ["east_a", "east_b"]
    assert eligible_routes(ROUTES, "north_1") == ()


def test_weight_zero_and_negative_skipped() -> None:
    chosen = {do_select(ROUTES, "east_1", seed).route_id for seed in range(200)}
    assert chosen <= {"east_a", "east_b"}
    assert "east_negative" not in chosen


def test_no_route_raises_route_unavailable() -> None:
    with pytest.raises(RouteUnavailable, match="north_1"):
        select_route(ROUTES, "north_1", seed=1)
    only_zero = (route("zero", "south_1", 0.0),)
    with pytest.raises(RouteUnavailable):
        select_route(only_zero, "south_1", seed=1)


def test_same_seed_selects_same_route() -> None:
    for seed in (0, 1, 2, 42, 2026, 2**31):
        assert do_select(ROUTES, "east_1", seed) is do_select(ROUTES, "east_1", seed)
    all_first = [do_select(ROUTES, "east_1", 123).route_id for _ in range(5)]
    assert len(set(all_first)) == 1


def test_rng_injection_is_deterministic() -> None:
    first = select_route(ROUTES, "east_1", rng=random.Random(99))
    second = select_route(ROUTES, "east_1", rng=random.Random(99))
    assert first == second
    other = select_route(ROUTES, "east_1", rng=random.Random(100))
    assert other in eligible_routes(ROUTES, "east_1")


def test_weighted_distribution_matches_weights() -> None:
    routes = (route("heavy", "s", 3.0), route("light", "s", 1.0))
    counts = {"heavy": 0, "light": 0}
    trials = 800
    for seed in range(trials):
        counts[select_route(routes, "s", seed=seed).route_id] += 1
    heavy_ratio = counts["heavy"] / trials
    # 理论比例 0.75，允许 ±0.08 的采样波动
    assert 0.67 <= heavy_ratio <= 0.83


def test_weight_extremes_influence_selection() -> None:
    routes = (route("dominant", "s", 1000.0), route("rare", "s", 0.001))
    hits = {select_route(routes, "s", seed=seed).route_id for seed in range(50)}
    assert "dominant" in hits


def test_select_route_for_map_error_mentions_map_id() -> None:
    pack = MapPack(
        map_id="map_alpha",
        name="Alpha",
        size_meters=(12000.0, 12000.0),
        world_bounds=(-6000.0, -6000.0, 6000.0, 6000.0),
        image_bounds=(0.0, 0.0, 1200.0, 1200.0),
        routes=ROUTES,
        spawns=(),
    )
    chosen = select_route_for_map(pack, "west_1", seed=3)
    assert chosen.route_id == "west_a"
    with pytest.raises(RouteUnavailable, match="map_alpha"):
        select_route_for_map(pack, "missing_spawn", seed=3)


def test_select_route_for_map_uses_pack_routes() -> None:
    pack = MapPack(
        map_id="map_beta",
        name="Beta",
        size_meters=(12000.0, 12000.0),
        world_bounds=(-6000.0, -6000.0, 6000.0, 6000.0),
        image_bounds=(0.0, 0.0, 1200.0, 1200.0),
        routes=(route("only", "spawn_a"),),
    )
    assert select_route_for_map(pack, "spawn_a", seed=0).route_id == "only"


def test_route_choice_serialization() -> None:
    choice = RouteChoice(route=route("a", "east_1"), spawn_id="east_1", candidates=2, seed=7, weight_total=1.6)
    data = choice.to_dict()
    assert data["route_id"] == "a"
    assert data["spawn_id"] == "east_1"
    assert data["candidates"] == 2
    assert data["weight_total"] == pytest.approx(1.6)
    assert data["seed"] == 7
