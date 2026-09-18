"""账单长截图的扫描、去重、红色标注框检测与 OCR。

本模块只做「图片 -> 文字块 / 标注框」，不含任何账单语义。
所有坐标都是原图像素坐标（左上为原点），因此跨切片的结果可以直接合并。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
SLICE_H = 3000
"""主切片高度。检测模型对整张 4 万像素高的长图会失效，必须切片。"""
STEP_H = 2600
"""切片步长，相邻切片保留 400px 重叠，避免行被切断。"""
TAIL_H = 2400
"""尾部补识别高度。高度 < 500px 的切片会被检测模型整体判空，所以尾部单独再跑一遍。"""

RED_MIN = 140
"""红色通道亮度下限。"""
RED_DOMINANCE = 60
"""红色必须比绿/蓝高出这么多，才算是「标注用的红」。"""
MARK_MIN_PIXELS = 120
"""一个标注框至少要有这么多红色像素，滤掉噪点。"""
MARK_MIN_SIDE = 24
"""标注框的长边下限，滤掉小红点。"""
MARK_GAP = 12
"""纵向间隔超过它就切开，认为换了一个标注框。"""
MARK_MAX_INSIDE = 0.15
"""标注框内部允许的红色占比上限。

手绘的标注框是「描边矩形」，内部基本是空的（只有黑色文字）；而手机状态栏里那些
橙红色的 App 图标是**实心色块**，内部也是红的。实测真框内部红占比 0.000、
状态栏图标 0.344，区分度足够。少了这条过滤，每张截图顶部的状态栏图标都会被误判成标注框。
"""

ProgressFn = Callable[[str], None]
Mark = tuple[float, float, float, float]
"""用户手绘的红色标注框：(左, 上, 右, 下)，原图像素坐标。"""


@dataclass(frozen=True)
class Item:
    """一个 OCR 文字块。"""

    y: float
    y2: float
    x: float
    x2: float
    t: str
    score: float

    @property
    def h(self) -> float:
        """文字块高度，用于区分「交易类型」大字与「时间」小字。"""
        return self.y2 - self.y


@dataclass
class Shot:
    """一张去重后的长截图及其识别结果。"""

    path: Path
    copies: list[Path] = field(default_factory=list)
    size: tuple[int, int] = (0, 0)
    items: list[Item] = field(default_factory=list)
    marks: list[Mark] = field(default_factory=list)
    """用户手绘的红色标注框；框住的行需要特别核对。"""


def md5(path: Path, chunk_size: int = 1 << 20) -> str:
    """计算文件内容 MD5。

    Args:
        path: 文件路径。
        chunk_size: 每次读取的字节数。

    Returns:
        32 位十六进制摘要。
    """
    digest = hashlib.md5()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def scan_images(folder: Path) -> list[Shot]:
    """列出目录下的图片并按内容去重。

    Args:
        folder: 图片所在目录（不递归）。

    Returns:
        每张唯一截图一个 Shot，副本路径记录在 ``copies`` 里，顺序按文件名。

    Raises:
        FileNotFoundError: 目录不存在。
    """
    if not folder.is_dir():
        raise FileNotFoundError(f"目录不存在：{folder}")
    buckets: dict[str, list[Path]] = {}
    for p in sorted(folder.iterdir()):
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            buckets.setdefault(md5(p), []).append(p)
    return [Shot(path=paths[0], copies=paths[1:]) for paths in buckets.values()]


def find_marks(image: Image.Image) -> list[Mark]:
    """找出图里用户手绘的红色标注框。

    用户会把「需要特别核对」的行用红框圈出来。红框的画法（描边矩形）没法直接当
    矩形读，所以这里先取红色像素掩码，再按纵向间隔聚类：每个簇的包围盒就是一个框。
    同一区域内多个框会并成一个，这不影响结果 —— 落到行上的归属是一样的。

    Args:
        image: 已打开的图片。

    Returns:
        ``(左, 上, 右, 下)`` 列表；没有红色标注时返回空列表。
    """
    rgb = np.asarray(image.convert("RGB"), dtype=np.int16)
    red, green, blue = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    mask = (red >= RED_MIN) & (red - green >= RED_DOMINANCE) & (red - blue >= RED_DOMINANCE)

    rows = np.flatnonzero(mask.any(axis=1))
    if rows.size == 0:
        return []

    bands: list[tuple[int, int]] = []
    start = previous = int(rows[0])
    for value in rows[1:]:
        value = int(value)
        if value - previous > MARK_GAP:
            bands.append((start, previous))
            start = value
        previous = value
    bands.append((start, previous))

    marks: list[Mark] = []
    for top, bottom in bands:
        band = mask[top : bottom + 1]
        columns = np.flatnonzero(band.any(axis=0))
        left, right = int(columns[0]), int(columns[-1])
        if band.sum() < MARK_MIN_PIXELS:
            continue
        if bottom - top < MARK_MIN_SIDE or right - left < MARK_MIN_SIDE:
            continue
        if _interior_ratio(band, left, right) > MARK_MAX_INSIDE:
            # 内部也是红的 → 实心色块（状态栏图标），不是手绘标注框
            continue
        marks.append((float(left), float(top), float(right), float(bottom)))
    return marks


def _interior_ratio(band: "np.ndarray", left: int, right: int) -> float:
    """标注框包围盒去掉外圈之后，内部红色像素的占比。"""
    region = band[:, left : right + 1]
    height, width = region.shape
    thickness = max(3, round(0.06 * min(width, height)))
    interior = region[
        thickness : max(thickness + 1, height - thickness),
        thickness : max(thickness + 1, width - thickness),
    ]
    return float(interior.mean()) if interior.size else 1.0


def ocr_shot(shot: Shot, engine: RapidOCR, progress: ProgressFn | None = None) -> Shot:
    """分块识别一张长截图，结果写入 ``shot.items`` / ``shot.marks``（原地修改）。

    Args:
        shot: 待识别的截图。
        engine: 复用的 RapidOCR 实例（构造开销大，不要每张图新建）。
        progress: 可选回调，用于向界面回报进度。

    Returns:
        传入的 shot（方便链式调用）。
    """
    image = Image.open(shot.path)
    width, height = image.size
    shot.size = (width, height)
    shot.marks = find_marks(image)
    items: list[Item] = []

    def run(y_from: int, y_to: int, keep_from: float, keep_to: float) -> None:
        """识别 [y_from, y_to) 区域，只保留 y 落在 [keep_from, keep_to) 的文字块。

        keep 区间用于让重叠区域只保留一份，避免同一行被两个切片重复计入。
        """
        result, _ = engine(np.array(image.crop((0, y_from, width, y_to))))
        for box, text, score in result or []:
            y = min(pt[1] for pt in box) + y_from
            y2 = max(pt[1] for pt in box) + y_from
            if keep_from <= y < keep_to:
                items.append(
                    Item(
                        y=y,
                        y2=y2,
                        x=min(pt[0] for pt in box),
                        x2=max(pt[0] for pt in box),
                        t=text,
                        score=float(score),
                    )
                )

    if height <= SLICE_H:
        # 单屏截图（例如收支详情页）：一次识别即可，也不必跑尾部补识别
        run(0, height, 0, float("inf"))
    else:
        y = 0
        while y < height:
            y_end = min(height, y + SLICE_H)
            run(y, y_end, y, y + STEP_H)
            if y_end >= height:
                break
            y += STEP_H

        tail_from = max(0, height - TAIL_H)
        if tail_from > 0:
            # 尾部切片太矮，主循环里会整体识别失败，这里用 2400px 的窗口重跑一次
            items = [it for it in items if it.y < tail_from]
            run(tail_from, height, tail_from, float("inf"))

    items.sort(key=lambda it: (it.y, it.x))
    shot.items = items
    if progress:
        progress(f"{shot.path.name}：{width}×{height}，{len(items)} 个文字块")
    return shot


def create_engine() -> RapidOCR:
    """构造 OCR 引擎。开销大（加载 onnx 模型），全流程复用同一个实例。"""
    return RapidOCR()


def selftest() -> None:
    """纯函数自检：红色标注框检测。不依赖 OCR，也不依赖任何真实截图。"""

    def canvas(height: int, width: int) -> "np.ndarray":
        return np.full((height, width, 3), 255, dtype=np.uint8)

    # 描边矩形 —— 手绘标注框，应当被认出来
    ring = canvas(120, 300)
    ring[20:100, 40:260] = (230, 70, 60)
    ring[26:94, 46:254] = 255
    found = find_marks(Image.fromarray(ring))
    assert len(found) == 1, found
    assert (found[0][0], found[0][1]) == (40.0, 20.0), found

    # 实心橙红色块 —— 手机状态栏里的 App 图标，不许被当成标注框
    blob = canvas(60, 120)
    blob[10:50, 20:100] = (235, 119, 56)
    assert find_marks(Image.fromarray(blob)) == [], "实心色块不该被当成标注框"

    # 一点红都没有时返回空
    assert find_marks(Image.fromarray(canvas(60, 120))) == []

    print("ocr.selftest OK")


if __name__ == "__main__":
    selftest()
