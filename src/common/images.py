"""本地 BGR 图片读取，兼容 Windows 中文目录与文件名。"""

from pathlib import Path

import cv2
import numpy as np


def read_color_image(path: Path) -> np.ndarray | None:
    """读取失败返回 None，由调用方报告相应资源的错误。"""
    try:
        data = path.read_bytes()
        if not data:
            return None
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    except (OSError, cv2.error):
        return None
    return image if image is not None and image.size else None
