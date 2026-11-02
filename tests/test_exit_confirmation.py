"""退出确认按钮两种实测布局的搜索区域回归，不操作真实游戏。"""

from pathlib import Path

import cv2
import numpy as np
import pytest
from src.common.types import Rect
from src.config.loader import load_project
from src.perception.capture import Frame
from src.perception.features import FeatureMatcher

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("center_y", [793, 857])
def test_exit_confirmation_covers_normal_and_early_end_layout(center_y: int) -> None:
    config = load_project(PROJECT_ROOT).config
    matcher = FeatureMatcher(config.profile, config.global_config.perception)
    template = cv2.imread(str(PROJECT_ROOT / "profiles/cn360/features/exit_confirm_yes.png"))
    assert template is not None
    image = np.full((1600, 2560, 3), 35, dtype=np.uint8)
    height, width = template.shape[:2]
    x, y = 1049 - width // 2, center_y - height // 2
    image[y:y + height, x:x + width] = template
    frame = Frame(image, 1.0, Rect(0, 0, 2560, 1600), 1)

    match = matcher.match("exit_confirm_yes", frame)

    assert match.found
    assert match.center == (1049, center_y)
