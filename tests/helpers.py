"""测试辅助工具：合成图像、YAML 写入与配置构造。

合成图像只用于离线单元测试，不代替实机标定；目的是让 ORB/模板匹配等逻辑
可以在没有游戏画面的情况下回归测试。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np
import yaml


def sample_background(width: int = 800, height: int = 800, seed: int = 7) -> np.ndarray:
    """生成带丰富角点/纹理的 BGR 合成底图，供 ORB 与模板匹配测试使用。"""
    rng = np.random.default_rng(seed)
    img = np.full((height, width, 3), 35, dtype=np.uint8)
    for _ in range(80):
        pts = rng.integers(0, [width, height], size=(3, 2)).astype(np.int32)
        color = tuple(int(v) for v in rng.integers(50, 255, 3))
        cv2.fillPoly(img, [pts], color)
    for _ in range(50):
        center = tuple(int(v) for v in rng.integers(0, [width, height], 2))
        radius = int(rng.integers(4, 24))
        color = tuple(int(v) for v in rng.integers(0, 255, 3))
        cv2.circle(img, center, radius, color, thickness=-1)
    for _ in range(30):
        p1 = tuple(int(v) for v in rng.integers(0, [width, height], 2))
        p2 = tuple(int(v) for v in rng.integers(0, [width, height], 2))
        color = tuple(int(v) for v in rng.integers(80, 255, 3))
        cv2.line(img, p1, p2, color, thickness=int(rng.integers(1, 5)))
    return img


def warp_image(
    image: np.ndarray,
    scale: float = 0.5,
    angle_deg: float = 0.0,
    tx: float = 0.0,
    ty: float = 0.0,
    output_size: tuple[int, int] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """按相似变换把图像变换到新画布，返回 (图像, 2x3 变换矩阵)。

    变换矩阵把原图像素映射到输出图像素，便于地图识别测试构造期望结果。
    """
    h, w = image.shape[:2]
    out_w, out_h = output_size or (w, h)
    matrix = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle_deg, scale)
    matrix[0, 2] += tx
    matrix[1, 2] += ty
    warped = cv2.warpAffine(
        image,
        matrix,
        (out_w, out_h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0),
    )
    return warped, matrix


def write_yaml(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path
