"""航线选择（详细设计 4.4 / 4.6）。

规则：
- 只使用绑定当前出生点的航线（``Route.spawn_id`` 一致）；
- 权重 ``weight <= 0`` 的航线跳过，用于调试阶段隔离未成熟航线；
- 同一出生点有多条航线时按权重随机选择；
- 随机数使用本场战斗 seed（``random.Random(seed)``），同一种子可复现选择结果；
- 没有可用航线时抛 :class:`~src.common.errors.RouteUnavailable`，不借用其他出生点的
  航线强行替代（详细设计 4.6）。
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..common.errors import RouteUnavailable
from ..config.models import MapPack, Route

__all__ = [
    "RouteChoice",
    "eligible_routes",
    "select_route",
    "select_route_for_map",
]


@dataclass(frozen=True, slots=True)
class RouteChoice:
    """一次航线选择的结果与依据，便于写入战斗日志。"""

    route: Route
    spawn_id: str
    candidates: int
    seed: int | None
    weight_total: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route.route_id,
            "spawn_id": self.spawn_id,
            "candidates": self.candidates,
            "weight_total": round(self.weight_total, 4),
            "seed": self.seed,
        }


def eligible_routes(routes: Sequence[Route], spawn_id: str) -> tuple[Route, ...]:
    """筛选某出生点下权重为正的候选航线（保持配置顺序）。"""
    return tuple(route for route in routes if route.spawn_id == spawn_id and route.weight > 0)


def _make_rng(seed: int | None, rng: random.Random | None) -> random.Random:
    if rng is not None:
        return rng
    if seed is None:
        return random.Random()
    return random.Random(seed)


def _choose(routes: Sequence[Route], spawn_id: str, seed: int | None, rng: random.Random | None) -> RouteChoice:
    candidates = eligible_routes(routes, spawn_id)
    if not candidates:
        total = sum(1 for route in routes if route.spawn_id == spawn_id)
        raise RouteUnavailable(
            f"出生点 {spawn_id!r} 没有可用航线（同出生点航线 {total} 条，权重均不为正）"
        )
    rng = _make_rng(seed, rng)
    total_weight = sum(route.weight for route in candidates)
    chosen = rng.choices(candidates, weights=[route.weight for route in candidates], k=1)[0]
    return RouteChoice(
        route=chosen,
        spawn_id=spawn_id,
        candidates=len(candidates),
        seed=seed,
        weight_total=total_weight,
    )


def select_route(
    routes: Sequence[Route],
    spawn_id: str,
    *,
    seed: int | None = None,
    rng: random.Random | None = None,
) -> Route:
    """按出生点加权选择一条航线；无可用航线时抛 RouteUnavailable。"""
    return _choose(routes, spawn_id, seed, rng).route


def select_route_for_map(
    map_pack: MapPack,
    spawn_id: str,
    *,
    seed: int | None = None,
    rng: random.Random | None = None,
) -> Route:
    """在一张地图包内按出生点选择航线（错误信息带地图编号）。"""
    try:
        return _choose(map_pack.routes, spawn_id, seed, rng).route
    except RouteUnavailable as exc:
        raise RouteUnavailable(f"地图 {map_pack.map_id!r}: {exc}") from exc
