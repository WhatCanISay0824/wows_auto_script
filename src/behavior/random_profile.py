"""本场随机档案（详细设计 8.3，评审 P1-6 倍率语义）。

每场战斗开始时生成一份随机档案，决定本场行为的整体倾向；档案的种子写入数据库，
复盘时可用同一种子复现本场全部行为随机数。

字段语义（评审 P1-6）：
- ``camera_activity``：视角**频率倍率**，越大越活跃；间隔均值 = ``camera_mean_s / camera_activity``；
- ``attack_frequency``：攻击**频率倍率**，越大越频繁；间隔均值 = ``attack_mean_s / attack_frequency``；
- ``consumable_ratio``：攻击事件选择消耗品的概率；
- ``long_stare_bias``：长时间凝视倾向，越大越容易出现长凝视；
- ``seed``：32 位整数种子。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from ..config.models import BehaviorConfig

_32BIT_MAX = 2**32


@dataclass(frozen=True, slots=True)
class RandomProfile:
    """一场战斗的随机行为档案（对应数据库 battle_random_profiles 表）。"""

    camera_activity: float
    attack_frequency: float
    consumable_ratio: float
    long_stare_bias: float
    seed: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "camera_activity": round(self.camera_activity, 4),
            "attack_frequency": round(self.attack_frequency, 4),
            "consumable_ratio": round(self.consumable_ratio, 4),
            "long_stare_bias": round(self.long_stare_bias, 4),
            "seed": self.seed,
        }

    @classmethod
    def generate(
        cls,
        config: BehaviorConfig,
        *,
        seed: int | None = None,
        rng: random.Random | None = None,
    ) -> RandomProfile:
        """生成一份随机档案。

        - ``seed`` 指定时用该种子派生全部字段（可复现）；
        - 未指定 ``seed`` 时从 ``rng``（默认系统随机源）取一个 32 位种子；
        - 字段采样使用 ``random.Random(seed)``，保证“只凭种子即可复现档案”。
        """
        if seed is None:
            source = rng if rng is not None else random.Random()
            seed = source.randrange(_32BIT_MAX)
        seed = int(seed) % _32BIT_MAX
        profile_rng = random.Random(seed)
        return cls(
            camera_activity=_sample_range(profile_rng, config.camera_activity_range, "camera_activity_range"),
            attack_frequency=_sample_range(profile_rng, config.attack_frequency_range, "attack_frequency_range"),
            consumable_ratio=_sample_range(profile_rng, config.consumable_ratio_range, "consumable_ratio_range"),
            long_stare_bias=_sample_range(profile_rng, config.long_stare_bias_range, "long_stare_bias_range"),
            seed=seed,
        )


def _sample_range(rng: random.Random, bounds: tuple[float, float], name: str) -> float:
    """在配置区间内均匀采样并钳位，保证结果一定落在区间内。"""
    low, high = float(bounds[0]), float(bounds[1])
    if high < low:
        raise ValueError(f"{name} 区间非法: {bounds!r}")
    value = rng.uniform(low, high)
    return min(max(value, low), high)
