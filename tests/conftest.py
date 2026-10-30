"""pytest 公共夹具。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from src.common.clock import FakeClock

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def background() -> np.ndarray:
    from tests.helpers import sample_background

    return sample_background()
