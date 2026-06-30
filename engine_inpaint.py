# -*- coding: utf-8 -*-
"""图片型页面的"区域擦除"(Stage 2): 人工/VL 框出产品页上的品牌(厂标/logo)→ 智能擦除, 看不出痕迹。
两档自动选(每个框独立判, 兼顾效果与速度):
  - 框周边背景均匀(纯色/简单, 如页眉白底/车身单色面) → 取周边中位色填充, 即时、无痕。
  - 框周边复杂(产品照纹理/渐变) → LaMa 图像修复(用周围内容重建), 最完美。
擦除后整页以高 DPI 图替换(图片型目录本就是图, 无损; 顺带把文本层也栅格化=对外更安全)。
"""
import io
import numpy as np
import fitz
from PIL import Image, ImageDraw

_LAMA = None
RENDER_DPI = 200          # 擦除时页面渲染 dpi(越高越清晰、越慢)
_UNIFORM_STD = 14         # 周边像素标准差阈值: 低于=均匀背景走纯色填充, 否则走 LaMa


def _get_lama():
    """懒加载 LaMa(首次会下载 ~200MB 模型)。无 torch/模型/加载失败时返回 None, 调用方退回纯色填充。
    ⚠️ simple-lama-inpainting 0.1.0 的 torch.jit.load 没传 map_location → CPU-only torch 上加载
    CUDA 存的模型会 NotImplementedError(aten::empty_strided CUDA backend)。这里临时 patch 强制 CPU 加载。"""
    global _LAMA
    if _LAMA is None:
        try:
            import torch
            from simple_lama_inpainting import SimpleLama
            _orig = torch.jit.load
            torch.jit.load = lambda f, *a, **k: _orig(f, map_location=torch.device("cpu"), *a, **{kk: vv for kk, vv in k.items() if kk != "map_location"})
            try:
                _LAMA = SimpleLama(device=torch.device("cpu"))
            finally:
                torch.jit.load = _orig
        except Exception:
            _LAMA = False
    return _LAMA or None


def _page_image(page, dpi=RENDER_DPI):
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _ring_stats(arr, x0, y0, x1, y1, pad):
    """框外一圈(pad 像素)像素的中位色 + 标准差, 用于判均匀背景 + 取填充色。"""
    H, W = arr.shape[:2]
    rx0, ry0 = max(0, x0 - pad), max(0, y0 - pad)
    rx1, ry1 = min(W, x1 + pad), min(H, y1 + pad)
    ring = arr[ry0:ry1, rx0:rx1].reshape(-1, 3).astype(np.float32)
    inner = arr[y0:y1, x0:x1].reshape(-1, 3).astype(np.float32)
    # 仅用"环减去内框"近似周边(数据量小, 直接用整块环的统计足够判均匀性)
    med = np.median(ring, axis=0)
    std = float(ring.std(axis=0).mean()) if len(ring) else 999
    return med, std


def inpaint_page(page, boxes_frac, dpi=RENDER_DPI):
    """boxes_frac: [[x0,y0,x1,y1], ...] 均为页面比例(0-1, 左上原点)。
    返回擦除后的 PIL.Image(整页, RGB)。每个框自动选 纯色填充 / LaMa。"""
    img = _page_image(page, dpi)
    W, H = img.size
    arr = np.array(img)
    px_boxes = []
    for b in boxes_frac:
        if len(b) != 4:
            continue
        x0, y0, x1, y1 = b
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        px = (int(max(0, x0) * W), int(max(0, y0) * H), int(min(1, x1) * W), int(min(1, y1) * H))
        if px[2] > px[0] and px[3] > px[1]:
            px_boxes.append(px)
    if not px_boxes:
        return img

    pad = max(8, int(0.01 * max(W, H)))
    lama_mask_boxes = []
    for (x0, y0, x1, y1) in px_boxes:
        med, std = _ring_stats(arr, x0, y0, x1, y1, pad)
        if std <= _UNIFORM_STD:
            arr[y0:y1, x0:x1] = med.astype(np.uint8)      # 均匀背景: 纯色填充, 即时无痕
        else:
            lama_mask_boxes.append((x0, y0, x1, y1))       # 复杂背景: 交给 LaMa

    img = Image.fromarray(arr)
    if lama_mask_boxes:
        lama = _get_lama()
        if lama is not None:
            mask = Image.new("L", (W, H), 0)
            d = ImageDraw.Draw(mask)
            grow = max(3, int(0.004 * max(W, H)))          # mask 略放大, 吃掉 logo 边缘抗锯齿
            for (x0, y0, x1, y1) in lama_mask_boxes:
                d.rectangle([x0 - grow, y0 - grow, x1 + grow, y1 + grow], fill=255)
            try:
                img = lama(img, mask).convert("RGB")
            except Exception:
                a2 = np.array(img)                          # LaMa 失败兜底: 纯色填充
                for (x0, y0, x1, y1) in lama_mask_boxes:
                    med, _ = _ring_stats(a2, x0, y0, x1, y1, pad)
                    a2[y0:y1, x0:x1] = med.astype(np.uint8)
                img = Image.fromarray(a2)
        else:
            a2 = np.array(img)                              # 无 LaMa: 纯色填充兜底
            for (x0, y0, x1, y1) in lama_mask_boxes:
                med, _ = _ring_stats(a2, x0, y0, x1, y1, pad)
                a2[y0:y1, x0:x1] = med.astype(np.uint8)
            img = Image.fromarray(a2)
    return img


def replace_page_with_image(page, pil_img):
    """用擦除后的图整页替换(opaque 全页覆盖原内容)。图片型目录本就是图, 视觉无损。"""
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    page.insert_image(page.rect, stream=buf.getvalue())
