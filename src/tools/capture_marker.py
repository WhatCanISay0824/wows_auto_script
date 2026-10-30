"""自身舰船标记标定 GUI（详细设计 12.1 / 12.3 / 5.7）。

用法：::

    python -m src.tools.capture_marker --root . --profile cn360 --screenshot 战斗截图.png

功能：

- 载入一张战斗截图（或从已录制的画面帧）；
- 在小地图区域内拖动矩形框选自身舰船标记；
- 保存 ``marker/marker.png`` 作为运行时模板（``ShipLocator`` 以它为 0° 基准，
  内部自动生成多角度版本），并额外输出 24 个角度旋转模板
  ``marker/marker_000.png`` … ``marker/marker_345.png`` 供人工检查；
- 更新 ``profile.yaml`` 的 ``minimap.marker_template`` 与 ``minimap.marker_angles``；
- 模板尺寸/颜色方差检查（纯色或过小的标记无法稳定匹配）。

旋转口径与 ``perception.ship_locate`` 一致：航向顺时针为正，OpenCV 旋转角取负值，
旋转画布扩大以完整保留模板内容。GUI 与逻辑分离，单元测试只验证纯函数。
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from . import calibrate_common as cc

#: 默认角度模板数（详细设计 5.7：0~345 度，每 15 度一个）
DEFAULT_MARKER_ANGLES = 24
#: 模板相对 profile 目录的默认路径（与 profiles/cn360/profile.yaml 一致）
DEFAULT_MARKER_TEMPLATE = "marker/marker.png"
MARKER_YAML_HEADER = """# 自身舰船标记模板目录（详细设计 5.7）
# marker.png 为 0° 基准模板；marker_XXX.png 为各航向角度的旋转版本（人工检查用）。
# 运行时由 src/perception/ship_locate.py 按 marker_angles 自动生成角度模板。"""


# ---------------------------------------------------------------------------
# 纯函数：旋转模板与保存
# ---------------------------------------------------------------------------


def expanded_size(image: np.ndarray) -> int:
    """旋转画布边长：能完整容纳任意角度旋转后的模板。"""
    height, width = image.shape[:2]
    return int(math.ceil(math.hypot(height, width))) + 2


def rotate_expanded(image: np.ndarray, heading_deg: float) -> np.ndarray:
    """绕模板中心旋转到指定航向（顺时针为正）并扩大到方形画布。

    与 ``perception.ship_locate._rotate_expanded`` 使用同一口径：
    ``angle_cv_deg = -heading``（OpenCV 逆时针为正）。
    """
    if image is None or image.size == 0:
        raise ValueError("标记模板为空，无法旋转")
    height, width = image.shape[:2]
    size = expanded_size(image)
    rotation = cv2.getRotationMatrix2D((width / 2.0, height / 2.0), -float(heading_deg), 1.0)
    rotation[0, 2] += size / 2.0 - width / 2.0
    rotation[1, 2] += size / 2.0 - height / 2.0
    return cv2.warpAffine(
        image,
        rotation,
        (size, size),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0),
    )


def generate_angle_templates(
    image: np.ndarray, angles: int = DEFAULT_MARKER_ANGLES
) -> list[tuple[float, np.ndarray]]:
    """生成 ``angles`` 个角度模板，返回 [(heading_deg, image), ...]。"""
    if angles <= 0:
        raise ValueError(f"角度模板数量必须大于 0: {angles}")
    step = 360.0 / int(angles)
    return [(index * step, rotate_expanded(image, index * step)) for index in range(int(angles))]


def marker_template_paths(marker_dir: Path | str, angles: int = DEFAULT_MARKER_ANGLES) -> list[Path]:
    """marker.png 与各角度模板的完整路径列表（不写文件）。"""
    directory = Path(marker_dir)
    step = 360.0 / int(angles)
    paths = [directory / "marker.png"]
    for index in range(int(angles)):
        heading = int(round(index * step))
        paths.append(directory / f"marker_{heading:03d}.png")
    return paths


def validate_marker_image(image: np.ndarray) -> list[str]:
    """标记模板质量检查；返回中文错误列表。"""
    quality = cc.inspect_template(image)
    errors = list(quality.errors)
    height, width = image.shape[:2]
    if width > 80 or height > 80:
        errors.append(f"标记模板 {width}x{height} 过大，小地图上的自身标记通常不超过 40x40")
    return errors


def save_marker_templates(
    marker_dir: Path | str,
    image: np.ndarray,
    *,
    angles: int = DEFAULT_MARKER_ANGLES,
) -> list[Path]:
    """保存 marker.png 与角度旋转模板，返回写入的文件列表；质量不合格抛 ``ValueError``。"""
    errors = validate_marker_image(image)
    if errors:
        raise ValueError("标记模板质量不合格: " + "；".join(errors))
    directory = Path(marker_dir)
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    base_path = directory / "marker.png"
    cc.write_image(base_path, image)
    written.append(base_path)
    for heading, rotated in generate_angle_templates(image, angles):
        path = directory / f"marker_{int(round(heading)):03d}.png"
        cc.write_image(path, rotated)
        written.append(path)
    cc.write_yaml_mapping(
        directory / "README.yaml",
        {
            "note": "自身舰船标记模板目录（详细设计 5.7）",
            "base_template": "marker.png",
            "angles": int(angles),
            "rotated_templates": [path.name for path in written[1:]],
        },
        header=MARKER_YAML_HEADER,
    )
    return written


def update_profile_marker(
    profile_path: Path | str,
    *,
    marker_template: str = DEFAULT_MARKER_TEMPLATE,
    angles: int = DEFAULT_MARKER_ANGLES,
) -> Path:
    """更新 profile.yaml 的 minimap.marker_template / minimap.marker_angles（保留其余字段与注释）。"""
    path = Path(profile_path)
    data = cc.read_yaml_mapping(path) if path.is_file() else {}
    minimap = data.get("minimap")
    if not isinstance(minimap, dict):
        minimap = {}
        data["minimap"] = minimap
    minimap["marker_template"] = marker_template
    minimap["marker_angles"] = int(angles)
    header = cc.read_leading_comments(path) or None
    return cc.write_yaml_mapping(path, data, header=header)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------


class MarkerCaptureApp:
    """自身标记标定主窗口（tkinter）；业务逻辑委托给本模块纯函数。"""

    def __init__(
        self,
        root: Any,
        *,
        marker_dir: Path,
        profile_path: Path | None,
        image: np.ndarray | None = None,
        image_path: Path | None = None,
        angles: int = DEFAULT_MARKER_ANGLES,
    ) -> None:
        tkinter = cc.import_tkinter()
        from tkinter import filedialog, messagebox  # noqa: PLC0415 - 延迟导入

        self._tk = tkinter
        self._filedialog = filedialog
        self._messagebox = messagebox
        self.root = root
        self.marker_dir = marker_dir
        self.profile_path = profile_path
        self.angles = int(angles)
        self.image = image
        self.image_path = image_path
        self.marker: np.ndarray | None = None
        self._build_ui()
        if image is not None:
            self.canvas.set_image(image)
        self._refresh_labels()

    def _build_ui(self) -> None:
        tk = self._tk
        main = tk.Frame(self.root)
        main.pack(fill="both", expand=True)
        left = tk.Frame(main, width=320)
        left.pack(side="left", fill="y", padx=6, pady=6)
        left.pack_propagate(False)
        tk.Label(left, text=f"标记目录: {self.marker_dir}", anchor="w", wraplength=300).pack(fill="x")
        if self.profile_path is not None:
            tk.Label(left, text=f"profile: {self.profile_path}", anchor="w", wraplength=300).pack(fill="x")
        self._info = tk.Label(left, text="", anchor="w", justify="left", fg="#0055aa", wraplength=300)
        self._info.pack(fill="x", pady=(4, 8))
        tk.Button(left, text="载入截图…", command=self._load_screenshot).pack(fill="x", pady=2)
        tk.Button(left, text="框选自身标记", command=self._start_box).pack(fill="x", pady=2)
        tk.Button(left, text="预览 24 角度模板", command=self._preview_angles).pack(fill="x", pady=2)
        tk.Button(left, text="保存标记模板", command=self._save).pack(fill="x", pady=(12, 2))
        self._hint = tk.Label(left, text="标记通常在右下角小地图内，是一条白色箭头", anchor="w", wraplength=300)
        self._hint.pack(fill="x")

        right = tk.Frame(main)
        right.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        self.canvas = cc.ImageCanvas(right, on_box=self._on_box_selected)
        self.canvas.frame.pack(fill="both", expand=True)
        self._status = tk.Label(right, anchor="w", fg="#995500")
        self._status.pack(fill="x")

    def _refresh_labels(self) -> None:
        frame = f"{self.image.shape[1]}x{self.image.shape[0]}" if self.image is not None else "无"
        marker = "已框选" if self.marker is not None else "未框选"
        self._info.configure(text=f"截图: {self.image_path or '未载入'}（{frame}）\n标记: {marker}\n角度模板: {self.angles}")

    def _load_screenshot(self) -> None:
        path = self._filedialog.askopenfilename(
            title="选择战斗截图",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp"), ("全部文件", "*.*")],
        )
        if not path:
            return
        try:
            self.image = cc.read_image(path)
            self.image_path = Path(path)
        except ValueError as exc:
            self._show_messages("截图读取失败", [str(exc)], error=True)
            return
        self.canvas.set_image(self.image)
        self._refresh_labels()

    def _start_box(self) -> None:
        if self.image is None:
            self._show_messages("缺少截图", ["请先点「载入截图…」或通过 --screenshot 指定"], error=True)
            return
        self.canvas.set_mode(cc.ImageCanvas.MODE_BOX)
        self._status.configure(text="拖动矩形框选自身舰船标记（尽量贴近标记边缘）")

    def _on_box_selected(self, selection: cc.BoxSelection) -> None:
        if self.image is None:
            return
        try:
            marker = cc.crop_region(self.image, selection)
        except ValueError as exc:
            self._show_messages("裁剪失败", [str(exc)], error=True)
            return
        errors = validate_marker_image(marker)
        if errors:
            self._show_messages("标记模板质量不合格", errors, error=True)
            return
        self.marker = marker
        quality = cc.inspect_template(marker)
        self._status.configure(
            text=f"已框选标记 {quality.summary()}；下一步可预览角度模板或直接保存"
        )
        print(f"[标记] {quality.summary()}，建议检查 {self.angles} 个角度模板后保存")
        self._refresh_labels()

    def _preview_angles(self) -> None:
        if self.marker is None:
            self._show_messages("尚未框选标记", ["请先框选自身标记"], error=True)
            return
        templates = generate_angle_templates(self.marker, self.angles)
        columns = 6
        cell = max(image.shape[0] for _, image in templates) + 8
        rows = math.ceil(len(templates) / columns)
        sheet = np.zeros((rows * cell, columns * cell, 3), dtype=np.uint8)
        for index, (heading, image) in enumerate(templates):
            row, column = divmod(index, columns)
            y0 = row * cell + 4
            x0 = column * cell + 4
            height, width = image.shape[:2]
            sheet[y0 : y0 + height, x0 : x0 + width] = image
            cv2.putText(
                sheet,
                f"{int(round(heading))}",
                (x0, y0 + height + 3),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.35,
                (200, 200, 200),
                1,
            )
        window = self._tk.Toplevel(self.root)
        window.title("角度模板预览（0~345 度，顺时针）")
        viewer = cc.ImageCanvas(window)
        viewer.frame.pack(fill="both", expand=True)
        viewer.set_image(sheet)
        self._tk.Label(window, text="旋转口径与 ship_locate 一致；运行时自动生成，无需单独加载").pack(fill="x")

    def _save(self) -> None:
        if self.marker is None:
            self._show_messages("尚未框选标记", ["请先框选自身标记"], error=True)
            return
        try:
            written = save_marker_templates(self.marker_dir, self.marker, angles=self.angles)
        except ValueError as exc:
            self._show_messages("保存失败", [str(exc)], error=True)
            return
        messages = [f"已保存 {len(written)} 个文件到 {self.marker_dir}"]
        if self.profile_path is not None and self.profile_path.is_file():
            try:
                update_profile_marker(
                    self.profile_path,
                    marker_template="marker/marker.png",
                    angles=self.angles,
                )
                messages.append(f"已更新 {self.profile_path}: minimap.marker_template=marker/marker.png，"
                                f"marker_angles={self.angles}")
            except (ValueError, OSError) as exc:
                messages.append(f"profile.yaml 更新失败（请手工填写 marker_template/marker_angles）: {exc}")
        self._show_messages("保存成功", messages)

    def _show_messages(self, title: str, messages: list[str], *, error: bool = False) -> None:
        text = "\n".join(str(message) for message in messages)
        print(f"[{title}]\n{text}")
        if error:
            self._messagebox.showerror(title, text)
        else:
            self._messagebox.showinfo(title, text)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.tools.capture_marker",
        description="自身舰船标记标定 GUI：从战斗截图框选标记，生成 marker.png 与多角度旋转模板",
    )
    parser.add_argument("--root", default=".", help="项目根目录（包含 config.yaml），默认当前目录")
    parser.add_argument("--profile", default="cn360", help="客户端配置编号，默认 cn360")
    parser.add_argument("--marker-dir", default=None, help="标记模板目录（默认 profiles/<profile>/marker）")
    parser.add_argument("--screenshot", default=None, help="战斗截图路径（启动后也可在界面载入）")
    parser.add_argument(
        "--angles",
        type=int,
        default=DEFAULT_MARKER_ANGLES,
        help=f"角度模板数量（默认 {DEFAULT_MARKER_ANGLES}，与 profile.minimap.marker_angles 一致）",
    )
    return parser


def resolve_marker_dir(args: argparse.Namespace) -> Path:
    if args.marker_dir:
        directory = Path(args.marker_dir).expanduser()
        if not directory.is_absolute():
            directory = Path(args.root).expanduser().resolve() / directory
        return directory
    return Path(args.root).expanduser().resolve() / "profiles" / args.profile / "marker"


def main(argv: list[str] | None = None) -> int:
    """标记标定入口：载入截图并打开 GUI。"""
    args = _build_parser().parse_args(argv)
    if args.angles <= 0:
        print(f"[ERROR] --angles 必须大于 0: {args.angles}", file=sys.stderr)
        return 2
    root_dir = Path(args.root).expanduser().resolve()
    if not root_dir.is_dir():
        print(f"[ERROR] 项目根目录不存在: {root_dir}", file=sys.stderr)
        return 2
    marker_dir = resolve_marker_dir(args)
    profile_path = root_dir / "profiles" / args.profile / "profile.yaml"
    image = None
    image_path = None
    if args.screenshot:
        image_path = Path(args.screenshot).expanduser()
        try:
            image = cc.read_image(image_path)
        except ValueError as exc:
            print(f"[ERROR] 截图读取失败: {exc}", file=sys.stderr)
            return 2
    try:
        root = cc.create_root(f"自身标记标定 - {args.profile}")
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    MarkerCaptureApp(
        root,
        marker_dir=marker_dir,
        profile_path=profile_path,
        image=image,
        image_path=image_path,
        angles=args.angles,
    )
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
