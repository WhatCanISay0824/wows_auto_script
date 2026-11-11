"""正式感知入口应能读取中文目录与文件名中的资源。"""

from pathlib import Path

import cv2
import numpy as np
import pytest
from src.common.types import NormalizedRect
from src.perception.map_identify import _read_background
from src.perception.ship_locate import ShipLocator
from src.perception.templates import compile_feature


@pytest.mark.parametrize("reader", ["feature", "background", "marker"])
def test_perception_loads_unicode_image_path(tmp_path: Path, reader: str) -> None:
    directory = tmp_path / "中文目录"
    directory.mkdir()
    path = directory / "船只模板.png"
    image = np.zeros((30, 30, 3), dtype=np.uint8)
    cv2.circle(image, (15, 15), 8, (240, 240, 240), 2)
    success, data = cv2.imencode(".png", image)
    assert success
    path.write_bytes(data.tobytes())

    if reader == "feature":
        feature, issues = compile_feature("target", path, NormalizedRect(0, 0, 1, 1))
        assert feature is not None, issues
        assert feature.template_size == (30, 30)
    else:
        loaded = _read_background(path) if reader == "background" else ShipLocator._load_template(path)
        np.testing.assert_array_equal(loaded, image)


@pytest.mark.parametrize("contents", [None, b"", b"not an image"])
def test_color_image_reader_rejects_missing_empty_or_corrupt_files(tmp_path: Path, contents: bytes | None) -> None:
    from src.common.images import read_color_image

    path = tmp_path / "损坏图片.png"
    if contents is not None:
        path.write_bytes(contents)

    assert read_color_image(path) is None
