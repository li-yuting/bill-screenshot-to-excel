"""账单长截图的扫描、去重与 OCR。

本模块只做「图片 -> 文字块」，不含任何账单语义。
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

ProgressFn = Callable[[str], None]


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


def ocr_shot(shot: Shot, engine: RapidOCR, progress: ProgressFn | None = None) -> Shot:
    """分块识别一张长截图，结果写入 ``shot.items``（原地修改）。

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
