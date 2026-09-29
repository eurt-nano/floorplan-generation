# -*- coding: utf-8 -*-
"""
户型生成与渲染

思路（对应 WallPlan, ACM TOG / SIGGRAPH 2022）：
    把一个户型的外轮廓当成一块矩形场地，每个房间是一个"N 站点"，
    房间的大小由 Power Diagram 的权重控制。
    给定每个房间的目标面积，反解权重，就得到了面积合规的户型划分。

    与传统"画格子"式户型生成相比，Power Diagram 的好处是
    分区边界天然是直线段、区域天然连通，不需要额外约束。

本模块负责：房间预设、配色、墙体与门窗绘制、文字标注。
"""
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from powerdiagram import cell_areas, optimize_weights, power_labels

FONT_CANDIDATES = [
    r'C:\Windows\Fonts\msyh.ttc',
    r'C:\Windows\Fonts\msyhbd.ttc',
    r'C:\Windows\Fonts\simhei.ttf',
]


def font(size, bold=False):
    order = ([FONT_CANDIDATES[1]] if bold else []) + FONT_CANDIDATES
    for p in order:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


# 按房间功能配色（低饱和，便于叠加文字）
ROOM_COLORS = {
    '客厅': (246, 231, 205), '餐厅': (250, 240, 214), '主卧': (208, 226, 246),
    '次卧': (219, 234, 250), '卧室': (208, 226, 246), '书房': (226, 218, 244),
    '厨房': (250, 219, 199), '卫生间': (206, 233, 233), '阳台': (216, 238, 214),
    '玄关': (232, 232, 232), '储物间': (238, 232, 222),
}
DEFAULT_COLOR = (235, 235, 235)

# 户型预设：名称、房间列表 (房间名, 目标面积占比, 初始位置[x,y])、实际尺寸(米)
PRESETS = {
    '一室一厅': dict(
        size=(9.0, 7.0),
        rooms=[('卧室', 0.28, (0.27, 0.26)), ('厨房', 0.14, (0.78, 0.22)),
               ('客厅', 0.32, (0.55, 0.65)), ('卫生间', 0.12, (0.80, 0.74)),
               ('阳台', 0.14, (0.12, 0.82))]),
    '两室一厅': dict(
        size=(11.0, 8.5),
        rooms=[('主卧', 0.22, (0.24, 0.25)), ('次卧', 0.16, (0.68, 0.22)),
               ('客厅', 0.26, (0.45, 0.68)), ('厨房', 0.12, (0.88, 0.48)),
               ('卫生间', 0.10, (0.88, 0.85)), ('阳台', 0.14, (0.10, 0.82))]),
    '三室一厅': dict(
        size=(13.0, 9.0),
        rooms=[('主卧', 0.20, (0.21, 0.23)), ('次卧', 0.15, (0.54, 0.20)),
               ('书房', 0.12, (0.85, 0.24)), ('客厅', 0.24, (0.40, 0.66)),
               ('厨房', 0.11, (0.86, 0.58)), ('卫生间', 0.09, (0.86, 0.88)),
               ('阳台', 0.09, (0.10, 0.86))]),
}


def dilate(mask, r):
    """
    形态学膨胀，把 1 像素宽的边界加粗成墙体。

    注意：这里必须先做 padding 再 roll。
    np.roll 是**环绕**的 —— 直接对原图 roll，会把图像另一侧的内容卷过来，
    结果外墙边缘会凭空出现"邻居"，门就被开到了外墙上。
    padding 之后，环绕进来的都是 False，等价于正常的边缘截断。
    """
    if r <= 0:
        return mask.copy()
    p = np.pad(mask, r, mode='constant', constant_values=False)
    out = p.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dy or dx:
                out |= np.roll(np.roll(p, dy, axis=0), dx, axis=1)
    return out[r:-r, r:-r]


def boundary_mask(labels):
    """找出区域边界：与左右或上下邻居标签不同的像素"""
    e = np.zeros(labels.shape, dtype=bool)
    e[:, 1:] |= labels[:, 1:] != labels[:, :-1]
    e[:, :-1] |= labels[:, 1:] != labels[:, :-1]
    e[1:, :] |= labels[1:, :] != labels[:-1, :]
    e[:-1, :] |= labels[1:, :] != labels[:-1, :]
    return e


def cell_centroids(labels, n):
    """每个区域的几何中心（用于放文字，比站点位置更居中）"""
    cs = []
    for i in range(n):
        ys, xs = np.nonzero(labels == i)
        cs.append((float(xs.mean()), float(ys.mean())) if len(xs) else (0.0, 0.0))
    return cs


def generate(preset, targets=None, W=900, iters=300, lr=0.4, opt_w=220):
    """
    生成一个户型。

    权重优化在**低分辨率**上进行（opt_w 宽），收敛后再按全分辨率重算标签图。
    因为迭代过程中只需要面积比例，低分辨率足够精确，速度却能快十倍以上。
    最终出的图仍是全分辨率，边缘不受影响。

    返回 dict：标签图、权重、房间信息（名称/目标面积/实际面积/中心）、画布尺寸
    """
    cfg = PRESETS[preset]
    aw, ah = cfg['size']
    H = int(round(W * ah / aw))                 # 保持户型真实长宽比，避免拉伸
    Ho = max(60, int(round(opt_w * ah / aw)))

    sites = np.array([r[2] for r in cfg['rooms']], dtype=np.float64)
    names = [r[0] for r in cfg['rooms']]
    # targets 可以覆盖预设里的面积（交互程序拖动滑块时用）
    if targets:
        tgt = np.array([float(targets.get(n, r[1])) for n, r in zip(names, cfg['rooms'])],
                       dtype=np.float64)
    else:
        tgt = np.array([r[1] for r in cfg['rooms']], dtype=np.float64)
    targets = tgt / tgt.sum()

    weights, _, _ = optimize_weights(sites, targets, opt_w, Ho, iters=iters, lr=lr)
    labels = power_labels(sites, weights, W, H)
    areas = cell_areas(labels, len(names))
    total_m2 = aw * ah
    centroids = cell_centroids(labels, len(names))

    rooms = [dict(name=names[i], target=float(targets[i]), actual=float(areas[i]),
                  site=tuple(sites[i]), center=centroids[i],
                  m2=float(areas[i] * total_m2))
             for i in range(len(names))]

    return dict(labels=labels, rooms=rooms, W=W, H=H,
                size=cfg['size'], total_m2=total_m2, preset=preset)


def render(fp, wall_px=3, doors=True, margin=26, title=None):
    """
    把生成结果画成户型图。

    绘制顺序：铺底色 -> 描墙 -> 开门洞 -> 画外框 -> 标文字
    """
    labels, rooms = fp['labels'], fp['rooms']
    W, H = fp['W'], fp['H']
    canvas = Image.new('RGB', (W + margin * 2, H + margin * 2 + (34 if title else 0)),
                       (255, 255, 255))
    ox = oy = margin
    if title:
        oy += 34

    arr = np.full((H, W, 3), 255, np.uint8)
    for i, r in enumerate(rooms):
        arr[labels == i] = ROOM_COLORS.get(r['name'], DEFAULT_COLOR)

    # 墙体
    wall = dilate(boundary_mask(labels), wall_px)
    arr[wall] = (70, 74, 80)

    # 门洞：相邻两房间的公共边界上开一个缺口
    if doors:
        edge_guard = wall_px + 14          # 距外墙这个距离以内的位置不开门
        for i in range(len(rooms)):
            for j in range(i + 1, len(rooms)):
                ys, xs = np.nonzero((labels == i) & dilate(labels == j, wall_px + 1))
                if len(xs) < 40:
                    continue
                # 只在"内部边界"上开门：剔除贴着外墙的候选点
                keep = ((xs > edge_guard) & (xs < W - edge_guard) &
                        (ys > edge_guard) & (ys < H - edge_guard))
                if keep.sum() < 20:
                    continue
                cx = float(np.median(xs[keep]))
                cy = float(np.median(ys[keep]))
                r = wall_px + 7
                y0, y1 = int(max(0, cy - r)), int(min(H, cy + r))
                x0, x1 = int(max(0, cx - r)), int(min(W, cx + r))
                sub = labels[y0:y1, x0:x1]
                open_ = (sub == i) | (sub == j)      # 只在该处属于这两个房间时开口
                arr[y0:y1, x0:x1][open_ & wall[y0:y1, x0:x1]] = \
                    ROOM_COLORS.get(rooms[i]['name'], DEFAULT_COLOR)

    canvas.paste(Image.fromarray(arr), (ox, oy))
    d = ImageDraw.Draw(canvas)

    # 外框
    d.rectangle([ox - 1, oy - 1, ox + W, oy + H], outline=(50, 54, 60), width=2)

    # 文字标注
    fn = font(17, True)
    fs = font(13)
    for r in rooms:
        cx, cy = r['center']
        cx, cy = ox + cx, oy + cy
        d.text((cx, cy - 11), r['name'], font=fn, fill=(40, 42, 48), anchor='mm')
        d.text((cx, cy + 11), '%.1f m²' % r['m2'], font=fs, fill=(96, 100, 108), anchor='mm')

    if title:
        d.text((ox, oy - 30), title, font=font(19, True), fill=(30, 32, 38))
    return canvas


def render_voronoi(fp, wall_px=3, margin=26, title=None):
    """等权重（退化为 Voronoi 图）的对照版本，用来展示权重的作用"""
    labels, rooms = fp['labels'], fp['rooms']
    W, H = fp['W'], fp['H']
    sites = np.array([r['site'] for r in rooms])
    lab0 = power_labels(sites, np.zeros(len(rooms)), W, H)

    canvas = Image.new('RGB', (W + margin * 2, H + margin * 2 + (34 if title else 0)),
                       (255, 255, 255))
    ox, oy = margin, margin + (34 if title else 0)
    arr = np.full((H, W, 3), 255, np.uint8)
    for i, r in enumerate(rooms):
        arr[lab0 == i] = ROOM_COLORS.get(r['name'], DEFAULT_COLOR)
    arr[dilate(boundary_mask(lab0), wall_px)] = (70, 74, 80)
    canvas.paste(Image.fromarray(arr), (ox, oy))
    d = ImageDraw.Draw(canvas)
    d.rectangle([ox - 1, oy - 1, ox + W, oy + H], outline=(50, 54, 60), width=2)
    if title:
        d.text((ox, oy - 30), title, font=font(19, True), fill=(30, 32, 38))
    return canvas
