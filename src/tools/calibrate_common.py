"""标定工具公共组件：图像查看器、坐标换算、方向预览与 YAML 读写（详细设计第 12 章）。

本模块是四个标定工具（calibrate_map / calibrate_profile / calibrate_ship /
capture_marker）的共享底座，分两层：

- **纯函数层**：``Viewport``（画布↔图像坐标）、``BoxSelection``（框选结果）、
  归一化矩形换算、8 方向预览（复用 ``common.coords`` 的 ``ImageOrientation`` /
  ``WorldImageTransform``）、带注释头的 YAML 读写。这一层不依赖 tkinter，
  可以在无图形会话环境下单元测试。
- **tkinter 层**：``ImageCanvas`` 把图像经 ``cv2.resize`` 后写成临时 PNG，
  用 ``tk.PhotoImage`` 显示，支持缩放、拖动与拖框选择；只在标定 GUI 里实例化，
  单元测试不创建窗口、不调用 ``mainloop``。

实现口径（详细设计 12.2/12.3 未写明的部分，记录为假设）：

1. 画布坐标 = 图像坐标 × ``zoom`` + 平移量，画布与图像都使用「左上角为原点、
   〞像素中心在整数坐标」的约定；框选结果统一为图像像素矩形 ``(x, y, w, h)``。
2. 方向预览把世界坐标按当前 ``image_orientation`` 换算成底图像素再绘制；
   ``canonicalize_image`` 把底图可游玩区域转成「正北朝上」画面，用于方向检查。
3. YAML 保存保留文件开头已有的注释块（模板说明），并按字段插入顺序输出。
"""

from __future__ import annotations

import contextlib
import math
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import yaml

from ..common.coords import ImageOrientation, WorldImageTransform, orientation_matrix
from ..common.types import NormalizedRect

#: 框选最小尺寸（图像像素），小于该值时视为误操作
DEFAULT_MIN_BOX_PX = 2
#: 缩放范围
MIN_ZOOM = 0.05
MAX_ZOOM = 20.0
#: 方向的中文说明（标定界面下拉框使用，顺序与 ImageOrientation 定义一致）
ORIENTATION_LABELS: Mapping[ImageOrientation, str] = {
    ImageOrientation.NORMAL: "正常（正北朝上）",
    ImageOrientation.ROT90: "顺时针旋转 90°",
    ImageOrientation.ROT180: "旋转 180°（上下颠倒）",
    ImageOrientation.ROT270: "顺时针旋转 270°",
    ImageOrientation.MIRROR_H: "左右镜像",
    ImageOrientation.MIRROR_V: "上下镜像",
    ImageOrientation.MIRROR_ROT90: "镜像 + 旋转 90°",
    ImageOrientation.MIRROR_ROT270: "镜像 + 旋转 270°",
}


# ---------------------------------------------------------------------------
# 画布坐标与框选
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Viewport:
    """图像在画布上的显示状态：缩放比例与平移量。

    ``offset_x / offset_y`` 是图像左上角在画布上的坐标（可以为负，表示拖出画面）。
    所有方法都是纯函数，返回新的 ``Viewport``，便于测试与撤销。
    """

    image_width: int
    image_height: int
    canvas_width: int
    canvas_height: int
    zoom: float = 1.0
    offset_x: float = 0.0
    offset_y: float = 0.0

    def __post_init__(self) -> None:
        if self.image_width <= 0 or self.image_height <= 0:
            raise ValueError(f"图像尺寸必须为正: {self.image_width}x{self.image_height}")
        if self.canvas_width <= 0 or self.canvas_height <= 0:
            raise ValueError(f"画布尺寸必须为正: {self.canvas_width}x{self.canvas_height}")
        if not math.isfinite(self.zoom) or self.zoom <= 0:
            raise ValueError(f"缩放比例必须为正数: {self.zoom!r}")

    # --- 坐标换算 -----------------------------------------------------
    def image_to_canvas(self, x: float, y: float) -> tuple[float, float]:
        """图像像素坐标 -> 画布坐标。"""
        return self.offset_x + x * self.zoom, self.offset_y + y * self.zoom

    def canvas_to_image(self, canvas_x: float, canvas_y: float) -> tuple[float, float]:
        """画布坐标 -> 图像像素坐标（可能落在图像外，调用方自行裁剪）。"""
        return (canvas_x - self.offset_x) / self.zoom, (canvas_y - self.offset_y) / self.zoom

    # --- 视图操作 -----------------------------------------------------
    def clamp_zoom(self, zoom: float) -> float:
        return min(MAX_ZOOM, max(MIN_ZOOM, float(zoom)))

    def fit(self, margin_ratio: float = 0.02) -> Viewport:
        """缩放到完整显示整张图并居中；``margin_ratio`` 为四周留白比例。"""
        margin = min(0.45, max(0.0, float(margin_ratio)))
        available_w = self.canvas_width * (1.0 - 2.0 * margin)
        available_h = self.canvas_height * (1.0 - 2.0 * margin)
        zoom = self.clamp_zoom(min(available_w / self.image_width, available_h / self.image_height))
        offset_x = (self.canvas_width - self.image_width * zoom) / 2.0
        offset_y = (self.canvas_height - self.image_height * zoom) / 2.0
        return replace(self, zoom=zoom, offset_x=offset_x, offset_y=offset_y)

    def zoom_at(self, canvas_x: float, canvas_y: float, factor: float) -> Viewport:
        """以画布上的某点为锚点缩放（该点下的图像像素保持不动）。"""
        if not math.isfinite(factor) or factor <= 0:
            raise ValueError(f"缩放倍数必须为正数: {factor!r}")
        image_x, image_y = self.canvas_to_image(canvas_x, canvas_y)
        zoom = self.clamp_zoom(self.zoom * float(factor))
        return replace(
            self,
            zoom=zoom,
            offset_x=canvas_x - image_x * zoom,
            offset_y=canvas_y - image_y * zoom,
        )

    def pan_by(self, delta_x: float, delta_y: float) -> Viewport:
        return replace(self, offset_x=self.offset_x + delta_x, offset_y=self.offset_y + delta_y)

    def visible_image_rect(self) -> BoxSelection:
        """当前画面上可见的图像区域（已裁剪到图像范围内）。"""
        left, top = self.canvas_to_image(0.0, 0.0)
        right, bottom = self.canvas_to_image(float(self.canvas_width), float(self.canvas_height))
        return clamp_selection(left, top, right, bottom, self.image_width, self.image_height)

    def with_canvas_size(self, canvas_width: int, canvas_height: int) -> Viewport:
        if canvas_width <= 0 or canvas_height <= 0:
            return self
        return replace(self, canvas_width=int(canvas_width), canvas_height=int(canvas_height))


@dataclass(frozen=True, slots=True)
class BoxSelection:
    """图像像素矩形（左上角 + 宽高），框选、区域输入的统一结果。"""

    x: int
    y: int
    w: int
    h: int

    def __post_init__(self) -> None:
        if self.w < 0 or self.h < 0:
            raise ValueError(f"矩形宽高不能为负: {self.w}x{self.h}")

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.w / 2.0, self.y + self.h / 2.0

    def to_tuple(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.w, self.h

    def normalized(self, image_width: int, image_height: int) -> NormalizedRect:
        """换算成归一化矩形（0～1），用于写回 profile.yaml / map.yaml。"""
        if self.w <= 0 or self.h <= 0:
            raise ValueError("空矩形无法换算为归一化坐标")
        if image_width <= 0 or image_height <= 0:
            raise ValueError("图像尺寸必须为正")
        x1 = min(1.0, max(0.0, self.x / image_width))
        y1 = min(1.0, max(0.0, self.y / image_height))
        x2 = min(1.0, max(0.0, self.right / image_width))
        y2 = min(1.0, max(0.0, self.bottom / image_height))
        x2 = max(x2, min(1.0, x1 + 1e-6))
        y2 = max(y2, min(1.0, y1 + 1e-6))
        return NormalizedRect(x1, y1, x2, y2)


def clamp_selection(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    image_width: int,
    image_height: int,
) -> BoxSelection:
    """把任意两点围成的矩形排序、取整并裁剪到图像范围内。"""
    left = int(round(min(x1, x2)))
    top = int(round(min(y1, y2)))
    right = int(round(max(x1, x2)))
    bottom = int(round(max(y1, y2)))
    left = max(0, min(image_width, left))
    top = max(0, min(image_height, top))
    right = max(0, min(image_width, right))
    bottom = max(0, min(image_height, bottom))
    return BoxSelection(left, top, max(0, right - left), max(0, bottom - top))


def selection_from_image_drag(
    start: tuple[float, float],
    end: tuple[float, float],
    image_width: int,
    image_height: int,
    *,
    min_size: int = DEFAULT_MIN_BOX_PX,
) -> BoxSelection | None:
    """图像坐标拖拽 -> 框选结果；小于 ``min_size`` 时返回 None（视为误点）。"""
    box = clamp_selection(start[0], start[1], end[0], end[1], image_width, image_height)
    if box.w < min_size or box.h < min_size:
        return None
    return box


def selection_from_canvas_drag(
    viewport: Viewport,
    start_canvas: tuple[float, float],
    end_canvas: tuple[float, float],
    *,
    min_size: int = DEFAULT_MIN_BOX_PX,
) -> BoxSelection | None:
    """画布坐标拖拽 -> 图像像素框选结果。"""
    start_image = viewport.canvas_to_image(start_canvas[0], start_canvas[1])
    end_image = viewport.canvas_to_image(end_canvas[0], end_canvas[1])
    return selection_from_image_drag(
        start_image,
        end_image,
        viewport.image_width,
        viewport.image_height,
        min_size=min_size,
    )


def selection_from_normalized(
    rect: NormalizedRect, image_width: int, image_height: int
) -> BoxSelection:
    """归一化矩形 -> 像素框选结果。"""
    pixels = rect.to_pixels(image_width, image_height)
    return clamp_selection(pixels.x, pixels.y, pixels.right, pixels.bottom, image_width, image_height)


# ---------------------------------------------------------------------------
# 文本解析（GUI 输入框与命令行共用，纯函数可测）
# ---------------------------------------------------------------------------


def parse_number(text: str) -> float:
    """解析一个有限数字；非法时抛 ``ValueError``（带中文说明）。"""
    try:
        value = float(str(text).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"不是合法数字: {text!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"数字必须是有限值: {text!r}")
    return value


def parse_number_pair(text: str) -> tuple[float, float]:
    """解析 ``"12000, 12000"`` 这样的两个数字。"""
    values = parse_number_list(text)
    if len(values) != 2:
        raise ValueError(f"需要 2 个数字（当前 {len(values)} 个）: {text!r}")
    return values[0], values[1]


def parse_number_quad(text: str) -> tuple[float, float, float, float]:
    """解析 ``"x1, y1, x2, y2"`` 这样的四个数字。"""
    values = parse_number_list(text)
    if len(values) != 4:
        raise ValueError(f"需要 4 个数字（当前 {len(values)} 个）: {text!r}")
    return values[0], values[1], values[2], values[3]


def parse_number_list(text: str) -> tuple[float, ...]:
    """按逗号/空格/分号拆分并解析数字列表。"""
    raw = str(text).replace(";", ",").replace("，", ",")
    parts = [item for item in raw.replace("\t", " ").replace(",", " ").split(" ") if item]
    if not parts:
        raise ValueError("数字列表为空")
    return tuple(parse_number(item) for item in parts)


def format_number(value: float, digits: int = 2) -> str:
    """格式化数字：去掉多余的 0 与小数点，便于写回 YAML 与界面显示。"""
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"数字必须是有限值: {value!r}")
    text = f"{number:.{max(0, int(digits))}f}".rstrip("0").rstrip(".")
    return text if text not in {"", "-0"} else "0"


def format_numbers(values: Sequence[float], digits: int = 2) -> str:
    return ", ".join(format_number(value, digits) for value in values)


def describe_normalized_rect(rect: NormalizedRect) -> str:
    return f"[{format_numbers(rect.to_list(), 4)}]"


# ---------------------------------------------------------------------------
# 方向预览（复用 common.coords 的 ImageOrientation / WorldImageTransform）
# ---------------------------------------------------------------------------


def orientation_label(orientation: ImageOrientation) -> str:
    return ORIENTATION_LABELS.get(orientation, orientation.value)


def orientation_from_label(label: str) -> ImageOrientation:
    """按中文说明反查方向；找不到时按枚举值解析（均失败抛 ``ValueError``）。"""
    for orientation, text in ORIENTATION_LABELS.items():
        if text == label:
            return orientation
    return ImageOrientation(str(label))


def build_world_transform(
    world_bounds: tuple[float, float, float, float],
    image_bounds: tuple[float, float, float, float],
    orientation: ImageOrientation,
) -> WorldImageTransform:
    """按 bounds + orientation 构造世界坐标 <-> 底图像素变换。"""
    return WorldImageTransform(
        world_bounds=world_bounds,
        image_bounds=image_bounds,
        orientation=orientation,
    )


def world_to_image_points(
    world_points: Sequence[tuple[float, float]],
    world_bounds: tuple[float, float, float, float],
    image_bounds: tuple[float, float, float, float],
    orientation: ImageOrientation,
) -> list[tuple[float, float]]:
    """把世界坐标点批量换算成底图像素（方向预览绘制用）。"""
    transform = build_world_transform(world_bounds, image_bounds, orientation)
    return [transform.world_to_image(x, y) for x, y in world_points]


def canonical_size_of(
    image_bounds: tuple[float, float, float, float], orientation: ImageOrientation
) -> tuple[int, int]:
    """底图可游玩区域在正北画面中的像素宽高（按方向交换宽高）。"""
    from ..common.coords import orientation_canonical_size

    width, height = orientation_canonical_size(image_bounds, orientation)
    return max(1, int(round(width))), max(1, int(round(height)))


def image_to_canonical_affine(
    image_bounds: tuple[float, float, float, float], orientation: ImageOrientation
) -> np.ndarray:
    """返回「整张底图像素 -> 正北朝上画面像素」的 3x3 仿射矩阵。

    与 :func:`canonicalize_image` 使用完全相同的换算，供标定工具把出生点/航线
    叠加到方向预览图上。
    """
    u_min, v_min, u_max, v_max = (float(value) for value in image_bounds)
    crop_w = u_max - u_min
    crop_h = v_max - v_min
    if crop_w <= 0 or crop_h <= 0:
        raise ValueError(f"image_bounds 无效: {image_bounds!r}")
    swap = orientation.swaps_axes
    out_w, out_h = (crop_h, crop_w) if swap else (crop_w, crop_h)
    out_to_canonical_inverse = np.array(
        [[out_w, 0.0, -0.5], [0.0, out_h, -0.5], [0.0, 0.0, 1.0]], dtype=np.float64
    )
    canonical_to_crop_inverse = np.array(
        [[1.0 / crop_w, 0.0, 0.5 / crop_w], [0.0, 1.0 / crop_h, 0.5 / crop_h], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    image_to_crop = np.array(
        [[1.0, 0.0, -u_min], [0.0, 1.0, -v_min], [0.0, 0.0, 1.0]], dtype=np.float64
    )
    return (
        out_to_canonical_inverse
        @ orientation_matrix(orientation)
        @ canonical_to_crop_inverse
        @ image_to_crop
    )


def canonicalize_image(
    image: np.ndarray,
    image_bounds: tuple[float, float, float, float],
    orientation: ImageOrientation,
) -> np.ndarray:
    """把底图可游玩区域转换成「正北朝上」画面（标定界面的方向预览）。

    返回图像尺寸为可游玩区域的 ``canonical_size``：交换宽高的方向（rot90/rot270
    及镜像组合）输出宽高互换。旋转/镜像都是 90 度整数倍，采用最近邻插值即可
    逐像素对齐。
    """
    if image is None or image.size == 0:
        raise ValueError("底图为空，无法转换方向预览")
    height, width = image.shape[:2]
    u_min, v_min, u_max, v_max = (float(value) for value in image_bounds)
    if not (0.0 <= u_min < u_max <= width and 0.0 <= v_min < v_max <= height):
        raise ValueError(f"image_bounds 在底图内无效: {image_bounds!r}，底图 {width}x{height}")
    if image_bounds[2] - image_bounds[0] <= 0 or image_bounds[3] - image_bounds[1] <= 0:
        raise ValueError(f"image_bounds 宽高必须为正: {image_bounds!r}")
    out_w, out_h = canonical_size_of(image_bounds, orientation)
    matrix = image_to_canonical_affine(image_bounds, orientation)
    return cv2.warpAffine(
        image,
        matrix[:2],
        (out_w, out_h),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_REPLICATE,
    )


def draw_points(
    image: np.ndarray,
    points: Sequence[tuple[float, float]],
    color: tuple[int, int, int] = (0, 220, 0),
    *,
    radius: int = 4,
    thickness: int = 1,
    labels: Sequence[str] | None = None,
) -> np.ndarray:
    """在图像副本上画点（附可选 ASCII 标签），返回新图。"""
    canvas = image.copy()
    for index, (x, y) in enumerate(points):
        center = (int(round(x)), int(round(y)))
        cv2.circle(canvas, center, int(radius), color, thickness=thickness, lineType=cv2.LINE_AA)
        if labels is not None and index < len(labels) and labels[index]:
            cv2.putText(
                canvas,
                str(labels[index]),
                (center[0] + radius + 2, center[1] - radius - 2),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                color,
                1,
                lineType=cv2.LINE_AA,
            )
    return canvas


def draw_polyline(
    image: np.ndarray,
    points: Sequence[tuple[float, float]],
    color: tuple[int, int, int] = (0, 180, 255),
    *,
    thickness: int = 2,
    closed: bool = False,
) -> np.ndarray:
    """在图像副本上画折线（航线预览），返回新图。"""
    canvas = image.copy()
    if len(points) < 2:
        return canvas
    polyline = np.array([[int(round(x)), int(round(y))] for x, y in points], dtype=np.int32)
    cv2.polylines(canvas, [polyline], bool(closed), color, thickness, lineType=cv2.LINE_AA)
    return canvas


# ---------------------------------------------------------------------------
# 图像读写（兼容非 ASCII 路径）
# ---------------------------------------------------------------------------


def read_image(path: Path | str) -> np.ndarray:
    """读取 BGR 图像；失败抛 ``ValueError``（中文原因）。"""
    file_path = Path(path)
    if not file_path.is_file():
        raise ValueError(f"图像文件不存在: {file_path}")
    data = np.fromfile(str(file_path), dtype=np.uint8)
    if data.size == 0:
        raise ValueError(f"图像文件为空: {file_path}")
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        raise ValueError(f"图像无法读取（不是有效图片）: {file_path}")
    return image


def write_image(path: Path | str, image: np.ndarray) -> Path:
    """写 PNG（``imencode`` + ``tofile``，兼容中文路径）。"""
    file_path = Path(path)
    if image is None or not isinstance(image, np.ndarray) or image.size == 0:
        raise ValueError("待保存图像为空")
    if image.dtype != np.uint8:
        raise ValueError(f"待保存图像必须是 uint8: {image.dtype}")
    file_path.parent.mkdir(parents=True, exist_ok=True)
    extension = file_path.suffix.lower() or ".png"
    success, buffer = cv2.imencode(extension, image)
    if not success:  # pragma: no cover - 取决于 OpenCV 编码器
        raise ValueError(f"图像编码失败: {file_path}")
    buffer.tofile(str(file_path))
    return file_path


#: 模板最小尺寸与最小灰度标准差（与 perception.templates 的校验口径一致）
MIN_TEMPLATE_SIZE = 8
MIN_TEMPLATE_STD = 2.0


@dataclass(frozen=True, slots=True)
class TemplateQuality:
    """模板质量检查结果（详细设计 12.3：模板过小或颜色单一容易误匹配）。"""

    width: int
    height: int
    std: float
    errors: tuple[str, ...] = ()
    hints: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        parts = [f"{self.width}x{self.height}", f"灰度标准差 {self.std:.2f}"]
        return "，".join(parts)


def crop_region(image: np.ndarray, box: BoxSelection) -> np.ndarray:
    """按框选结果裁剪图像（返回副本）；越界或空框抛 ``ValueError``。"""
    if image is None or image.size == 0:
        raise ValueError("图像为空，无法裁剪")
    height, width = image.shape[:2]
    if box.w <= 0 or box.h <= 0:
        raise ValueError(f"裁剪区域为空: {box!r}")
    if box.x < 0 or box.y < 0 or box.right > width or box.bottom > height:
        raise ValueError(f"裁剪区域超出图像范围: {box!r}，图像 {width}x{height}")
    return image[box.y : box.bottom, box.x : box.right].copy()


def inspect_template(image: np.ndarray) -> TemplateQuality:
    """检查候选模板的尺寸与颜色方差，给出错误与改进建议。"""
    if image is None or not isinstance(image, np.ndarray) or image.size == 0:
        return TemplateQuality(0, 0, 0.0, errors=("模板为空",))
    height, width = image.shape[:2]
    if image.ndim == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    elif image.ndim == 2:
        gray = image
    else:
        return TemplateQuality(width, height, 0.0, errors=(f"不支持的图像形状: {image.shape}",))
    std = float(gray.std())
    errors: list[str] = []
    hints: list[str] = []
    if width < MIN_TEMPLATE_SIZE or height < MIN_TEMPLATE_SIZE:
        errors.append(
            f"模板尺寸 {width}x{height} 小于 {MIN_TEMPLATE_SIZE}x{MIN_TEMPLATE_SIZE}，请框选更大的区域"
        )
    if std < MIN_TEMPLATE_STD:
        errors.append(f"模板接近纯色（灰度标准差 {std:.2f} < {MIN_TEMPLATE_STD}），归一化匹配会大量误匹配")
    if width < 16 or height < 16:
        hints.append("模板偏小，建议包含按钮周围的固定背景")
    if width > 400 or height > 400:
        hints.append("模板较大，匹配耗时会增加；确认只包含需要的按钮区域")
    hints.append("模板应包含周围的固定背景，不要只截按钮内部（详细设计 12.3）")
    return TemplateQuality(width, height, std, errors=tuple(errors), hints=tuple(hints))


# ---------------------------------------------------------------------------
# YAML 读写（保留注释模板，sort_keys=False）
# ---------------------------------------------------------------------------


def read_leading_comments(path: Path | str) -> str:
    """读取文件开头的注释块（连续的注释行与空行），用于保存时保留模板说明。"""
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            lines.append(line.rstrip())
            continue
        break
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def dump_yaml_mapping(
    data: Mapping[str, Any],
    *,
    header: str | None = None,
    sort_keys: bool = False,
) -> str:
    """把映射转成 YAML 文本；``header`` 原样放在最前面（注释模板）。"""
    if not isinstance(data, Mapping):
        raise ValueError("YAML 顶层必须是映射（mapping）")
    body = yaml.safe_dump(
        dict(data),
        allow_unicode=True,
        sort_keys=sort_keys,
        default_flow_style=False,
        width=120,
    )
    if not header:
        return body
    prefix = header.rstrip("\n")
    return f"{prefix}\n\n{body}" if prefix else body


def write_yaml_mapping(
    path: Path | str,
    data: Mapping[str, Any],
    *,
    header: str | None = None,
    sort_keys: bool = False,
) -> Path:
    """写 YAML：``header`` 为 None 时自动沿用文件已有的注释块。"""
    file_path = Path(path)
    resolved_header = header
    if resolved_header is None and file_path.is_file():
        existing = read_leading_comments(file_path)
        resolved_header = existing or None
    text = dump_yaml_mapping(data, header=resolved_header, sort_keys=sort_keys)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(text, encoding="utf-8")
    return file_path


def read_yaml_mapping(path: Path | str) -> dict[str, Any]:
    """读取 YAML 映射；文件不存在/语法错误/顶层不是映射时抛 ``ValueError``。"""
    file_path = Path(path)
    if not file_path.is_file():
        raise ValueError(f"YAML 文件不存在: {file_path}")
    try:
        data = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML 解析失败: {file_path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML 顶层必须是映射（mapping）: {file_path}")
    return data


# ---------------------------------------------------------------------------
# tkinter 图像画布（只在 GUI 工具中使用）
# ---------------------------------------------------------------------------


def import_tkinter() -> Any:
    """导入 tkinter 模块；不可用时抛 ``RuntimeError``（中文原因）。"""
    try:
        import tkinter  # noqa: PLC0415 - 延迟导入，避免无图形环境下 --help 失败
    except Exception as exc:  # noqa: BLE001 - 导入失败原因多样
        raise RuntimeError(f"tkinter 不可用，无法启动标定界面: {exc}") from exc
    return tkinter


def create_root(title: str) -> Any:
    """创建并配置 Tk 根窗口（不进入 mainloop，由调用方负责）。"""
    tkinter = import_tkinter()
    try:
        root = tkinter.Tk()
    except Exception as exc:  # noqa: BLE001 - 无显示会话等原因
        raise RuntimeError(f"无法创建图形窗口（请在有桌面会话的环境中运行）: {exc}") from exc
    root.title(title)
    with contextlib.suppress(Exception):
        root.geometry("1200x800")
    return root


@dataclass(frozen=True, slots=True)
class Annotation:
    """画布叠加物：点、线或文字（坐标均为图像像素）。"""

    kind: str  # "point" / "line" / "text"
    points: tuple[tuple[float, float], ...] = ()
    color: str = "#00dd66"
    text: str = ""
    radius: int = 4


class _TemporaryPng:
    """把 numpy 图像写成临时 PNG，供 ``tk.PhotoImage`` 加载（Windows 中文路径安全）。"""

    def __init__(self) -> None:
        self._directory = Path(tempfile.mkdtemp(prefix="wows_calib_"))
        self._path = self._directory / "view.png"
        self._counter = 0

    @property
    def directory(self) -> Path:
        return self._directory

    def write(self, image: np.ndarray) -> Path:
        self._counter += 1
        path = self._directory / f"view_{self._counter}.png"
        write_image(path, image)
        self._path = path
        return path

    def close(self) -> None:
        self._path = self._directory / "view.png"
        try:
            for entry in self._directory.iterdir():
                entry.unlink(missing_ok=True)
            self._directory.rmdir()
        except OSError:  # pragma: no cover - 临时目录清理失败不影响主流程
            pass


class ImageCanvas:
    """带缩放/拖动/拖框选择的图像画布（标定工具共用，不进入 mainloop）。

    用法::

        canvas = ImageCanvas(parent, on_box=self._on_box_selected)
        canvas.set_image(background)          # 自动 fit
        canvas.set_mode(ImageCanvas.MODE_BOX) # 左键拖框
        canvas.set_mode(ImageCanvas.MODE_CLICK)  # 左键单击回调图像坐标

    模式说明：
    - ``MODE_PAN``：左键/右键拖动平移画面；
    - ``MODE_BOX``：左键拖框，松开时回调 ``on_box(BoxSelection)``；
    - ``MODE_CLICK``：左键单击回调 ``on_click((x, y))``（图像像素）。
    """

    MODE_PAN = "pan"
    MODE_BOX = "box"
    MODE_CLICK = "click"

    def __init__(
        self,
        master: Any,
        *,
        width: int = 980,
        height: int = 640,
        on_box: Any = None,
        on_click: Any = None,
        on_cursor: Any = None,
    ) -> None:
        tkinter = import_tkinter()
        self._tk = tkinter
        self._on_box = on_box
        self._on_click = on_click
        self._on_cursor = on_cursor
        self.frame = tkinter.Frame(master)
        self.canvas = tkinter.Canvas(
            self.frame,
            width=width,
            height=height,
            background="#1e1e1e",
            highlightthickness=0,
        )
        self.canvas.pack(side="top", fill="both", expand=True)
        self.status = tkinter.Label(self.frame, anchor="w", justify="left", text="")
        self.status.pack(side="bottom", fill="x")

        self._image: np.ndarray | None = None
        self._viewport: Viewport | None = None
        self._mode = self.MODE_PAN
        self._drag_start: tuple[float, float] | None = None
        self._drag_last: tuple[float, float] | None = None
        self._drag_rect_id: int | None = None
        self._annotations: tuple[Annotation, ...] = ()
        self._png = _TemporaryPng()
        self._photo: Any = None
        self._canvas_size = (int(width), int(height))

        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<ButtonPress-1>", self._on_left_press)
        self.canvas.bind("<B1-Motion>", self._on_left_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_left_release)
        self.canvas.bind("<ButtonPress-3>", self._on_right_press)
        self.canvas.bind("<B3-Motion>", self._on_right_motion)
        self.canvas.bind("<MouseWheel>", self._on_wheel)

    # --- 查询 ---------------------------------------------------------
    @property
    def image(self) -> np.ndarray | None:
        return self._image

    @property
    def viewport(self) -> Viewport | None:
        return self._viewport

    @property
    def mode(self) -> str:
        return self._mode

    def image_to_canvas(self, x: float, y: float) -> tuple[float, float]:
        viewport = self._require_viewport()
        return viewport.image_to_canvas(x, y)

    def canvas_to_image(self, canvas_x: float, canvas_y: float) -> tuple[float, float]:
        viewport = self._require_viewport()
        return viewport.canvas_to_image(canvas_x, canvas_y)

    def _require_viewport(self) -> Viewport:
        if self._viewport is None:
            raise RuntimeError("画布尚未载入图像")
        return self._viewport

    # --- 图像与模式 ---------------------------------------------------
    def set_image(self, image: np.ndarray) -> None:
        if image is None or image.size == 0:
            raise ValueError("待显示图像为空")
        self._image = image
        height, width = image.shape[:2]
        self._viewport = Viewport(
            image_width=width,
            image_height=height,
            canvas_width=self._canvas_size[0],
            canvas_height=self._canvas_size[1],
        ).fit()
        self.redraw()

    def load_path(self, path: Path | str) -> np.ndarray:
        image = read_image(path)
        self.set_image(image)
        return image

    def set_mode(self, mode: str) -> None:
        if mode not in {self.MODE_PAN, self.MODE_BOX, self.MODE_CLICK}:
            raise ValueError(f"未知画布模式: {mode!r}")
        self._mode = mode
        self._update_status()

    def set_annotations(self, annotations: Sequence[Annotation]) -> None:
        self._annotations = tuple(annotations)
        self.redraw()

    def fit(self) -> None:
        if self._viewport is None:
            return
        self._viewport = self._viewport.fit()
        self.redraw()

    def zoom_by(self, factor: float) -> None:
        if self._viewport is None:
            return
        center = (self._canvas_size[0] / 2.0, self._canvas_size[1] / 2.0)
        self._viewport = self._viewport.zoom_at(center[0], center[1], factor)
        self.redraw()

    def close(self) -> None:
        self._png.close()

    # --- 事件 ---------------------------------------------------------
    def _canvas_xy(self, event: Any) -> tuple[float, float]:
        return float(event.x), float(event.y)

    def _on_configure(self, event: Any) -> None:
        size = (max(1, int(event.width)), max(1, int(event.height)))
        if size == self._canvas_size:
            return
        self._canvas_size = size
        if self._viewport is not None:
            self._viewport = self._viewport.with_canvas_size(*size)
            self.redraw()

    def _on_left_press(self, event: Any) -> None:
        if self._viewport is None:
            return
        position = self._canvas_xy(event)
        if self._mode == self.MODE_CLICK:
            if self._on_click is not None:
                self._on_click(self._viewport.canvas_to_image(*position))
            return
        if self._mode == self.MODE_BOX:
            self._drag_start = position
            self._drag_last = position
            self._drag_rect_id = None
        else:
            self._drag_start = position
            self._drag_last = position

    def _on_left_motion(self, event: Any) -> None:
        position = self._canvas_xy(event)
        if self._viewport is not None and self._on_cursor is not None:
            self._on_cursor(self._viewport.canvas_to_image(*position))
        if self._viewport is None or self._drag_start is None:
            return
        if self._mode == self.MODE_BOX:
            self._drag_last = position
            self._draw_drag_rect()
        elif self._mode == self.MODE_PAN:
            delta = (position[0] - self._drag_last[0], position[1] - self._drag_last[1])
            self._drag_last = position
            self._viewport = self._viewport.pan_by(*delta)
            self.redraw()

    def _on_left_release(self, event: Any) -> None:
        if self._viewport is None or self._drag_start is None:
            return
        position = self._canvas_xy(event)
        start = self._drag_start
        self._drag_start = None
        self._drag_last = None
        if self._drag_rect_id is not None:
            self.canvas.delete(self._drag_rect_id)
            self._drag_rect_id = None
        if self._mode == self.MODE_BOX and self._on_box is not None:
            selection = selection_from_canvas_drag(self._viewport, start, position)
            if selection is not None:
                self._on_box(selection)
        self.redraw()

    def _on_right_press(self, event: Any) -> None:
        self._drag_last = self._canvas_xy(event)

    def _on_right_motion(self, event: Any) -> None:
        if self._viewport is None or self._drag_last is None:
            return
        position = self._canvas_xy(event)
        delta = (position[0] - self._drag_last[0], position[1] - self._drag_last[1])
        self._drag_last = position
        self._viewport = self._viewport.pan_by(*delta)
        self.redraw()

    def _on_wheel(self, event: Any) -> None:
        if self._viewport is None:
            return
        steps = float(event.delta) / 120.0
        if abs(steps) < 1e-6:
            steps = 1.0 if event.delta > 0 else -1.0
        factor = 1.1**steps
        position = self._canvas_xy(event)
        self._viewport = self._viewport.zoom_at(position[0], position[1], factor)
        self.redraw()

    # --- 绘制 ---------------------------------------------------------
    def _draw_drag_rect(self) -> None:
        if self._drag_start is None or self._drag_last is None:
            return
        x1, y1 = self._drag_start
        x2, y2 = self._drag_last
        if self._drag_rect_id is None:
            self._drag_rect_id = self.canvas.create_rectangle(
                x1, y1, x2, y2, outline="#ffcc00", width=2, dash=(4, 2)
            )
        else:
            self.canvas.coords(self._drag_rect_id, x1, y1, x2, y2)

    def redraw(self) -> None:
        """按当前视口把可见区域栅格化后显示；叠加物按图像坐标绘制。"""
        self.canvas.delete("all")
        self._drag_rect_id = None
        if self._image is None or self._viewport is None:
            self._update_status()
            return
        viewport = self._viewport
        visible = viewport.visible_image_rect()
        if visible.w <= 0 or visible.h <= 0:
            self._update_status()
            return
        source = self._image[visible.y : visible.bottom, visible.x : visible.right]
        target_w = max(1, int(round(visible.w * viewport.zoom)))
        target_h = max(1, int(round(visible.h * viewport.zoom)))
        interpolation = cv2.INTER_AREA if viewport.zoom < 1.0 else cv2.INTER_LINEAR
        rendered = cv2.resize(source, (target_w, target_h), interpolation=interpolation)
        canvas_x, canvas_y = viewport.image_to_canvas(visible.x, visible.y)
        path = self._png.write(rendered)
        try:
            photo = self._tk.PhotoImage(file=str(path))
        except Exception:  # pragma: no cover - 取决于 Tk 构建是否支持 PNG
            photo = None
        if photo is not None:
            self._photo = photo
            self.canvas.create_image(int(round(canvas_x)), int(round(canvas_y)), anchor="nw", image=photo)
        for annotation in self._annotations:
            self._draw_annotation(annotation)
        self._update_status()

    def _draw_annotation(self, annotation: Annotation) -> None:
        if self._viewport is None:
            return
        points = [self._viewport.image_to_canvas(x, y) for x, y in annotation.points]
        if annotation.kind == "point":
            for x, y in points:
                self.canvas.create_oval(
                    x - annotation.radius,
                    y - annotation.radius,
                    x + annotation.radius,
                    y + annotation.radius,
                    outline=annotation.color,
                    width=2,
                )
                if annotation.text:
                    self.canvas.create_text(
                        x + annotation.radius + 4,
                        y - annotation.radius - 4,
                        text=annotation.text,
                        fill=annotation.color,
                        anchor="sw",
                    )
        elif annotation.kind == "line" and len(points) >= 2:
            flat: list[float] = []
            for x, y in points:
                flat.extend((x, y))
            self.canvas.create_line(*flat, fill=annotation.color, width=2)
        elif annotation.kind == "text" and points:
            self.canvas.create_text(
                points[0][0],
                points[0][1],
                text=annotation.text,
                fill=annotation.color,
                anchor="nw",
            )

    def _update_status(self) -> None:
        if self._viewport is None:
            self.status.configure(text="未载入图像")
            return
        text = (
            f"模式: {self._mode}    缩放: {self._viewport.zoom:.3f}    "
            f"滚轮缩放，右键/平移模式拖动。框选坐标以原始图像像素为准。"
        )
        self.status.configure(text=text)


# ---------------------------------------------------------------------------
# 命令行入口（公共组件本身没有独立功能，提供 --help 便于工具链自检）
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """公共组件入口：只打印用途说明并返回 0（真正的标定命令见其它工具）。"""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m src.tools.calibrate_common",
        description=(
            "标定工具公共组件（图像画布、坐标换算、方向预览、YAML 读写）。"
            "独立命令行入口有：calibrate_map / calibrate_profile / calibrate_ship / capture_marker。"
        ),
    )
    parser.parse_args(argv)
    print("calibrate_common 是标定工具共享库，请使用 calibrate_map / calibrate_profile / "
          "calibrate_ship / capture_marker 启动具体标定界面。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
